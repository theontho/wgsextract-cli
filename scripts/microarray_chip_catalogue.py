"""Build the packaged microarray chip-revision catalogue from public sources.

  corpus  Stream genotype files of the openSNP 2017-12-08 dump (CC0) with HTTP
          range requests and write one marker fingerprint per file (JSONL) plus
          each distinct marker-site list. Nothing else from a file is kept:
          no genotypes, names, IDs or header text.
  build   Group the corpus by marker multiset, merge it with the installed
          templates and write ``assets/microarray/revisions/catalogue.json``.

Usage:
  python scripts/microarray_chip_catalogue.py corpus WORKDIR
  python scripts/microarray_chip_catalogue.py build WORKDIR --templates REFLIB
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import gzip
import io
import json
import queue
import re
import sys
import threading
import time
import urllib.request
import zipfile
import zlib
from pathlib import Path
from typing import Any, TextIO

from wgsextract_cli.core.microarray_revisions import (
    _COLUMN_LABELS,
    CATALOGUE,
    Fingerprint,
    RawExport,
    decode_text,
    encode_sketch,
    estimate_jaccard,
    fingerprint,
    parse_raw_export,
)
from wgsextract_cli.core.microarray_utils import (
    _resolve_templates_for_format,
    get_template_format,
    template_rows,
)
from wgsextract_cli.core.utils import WGSExtractError

DUMP_URL = (
    "https://archive.org/download/opensnp_data_dumps/opensnp_datadump.2017-12-08.zip"
)
DUMP_SHA1 = "c266c3d53049c41817eb95b608e547321bb35feb"
DUMP_SOURCE = (
    "openSNP data dump 2017-12-08, CC0 1.0 "
    "(https://archive.org/details/opensnp_data_dumps)"
)
# openSNP's upload labels; the file's own header decides the vendor.
DUMP_LABELS = ("23andme", "ancestry", "ftdna-illumina")
MIN_EXPORTS = 3
# Chip exports are under 40 MB; larger uploads are imputed or sequencing data.
MAX_MEMBER_BYTES = 64 << 20


class _HttpRange(io.RawIOBase):
    def __init__(self, url: str) -> None:
        self.source = url
        self.pos = 0
        self._resolve()

    def _resolve(self) -> None:
        # archive.org redirects to a storage node; a failing node is re-resolved.
        request = urllib.request.Request(self.source, method="HEAD")
        with urllib.request.urlopen(request, timeout=300) as response:
            self.url = response.geturl()
            self.size = int(response.headers["Content-Length"])

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        base = (0, self.pos, self.size)[whence]
        self.pos = base + offset
        return self.pos

    def readinto(self, buffer: Any) -> int:
        size = min(len(buffer), self.size - self.pos)
        if size <= 0:
            return 0
        span = {"Range": f"bytes={self.pos}-{self.pos + size - 1}"}
        for attempt in range(8):
            try:
                request = urllib.request.Request(self.url, headers=span)
                with urllib.request.urlopen(request, timeout=300) as response:
                    data = response.read()
                if len(data) == size:
                    break
            except OSError as error:
                print(f"retry {attempt + 1}: {error}", file=sys.stderr)
                time.sleep(min(2**attempt, 120))
                with contextlib.suppress(OSError):
                    self._resolve()
        else:
            raise OSError("range request failed")
        buffer[:size] = data
        self.pos += size
        return size


def _member_text(data: bytes) -> TextIO:
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    elif data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as inner:
            names = [
                n
                for n in inner.namelist()
                if not n.endswith("/")
                and "__MACOSX" not in n
                and not Path(n).name.startswith(".")
            ]
            if len(names) != 1:
                raise WGSExtractError("nested ZIP without exactly one file")
            data = inner.read(names[0])
    return io.StringIO(decode_text(data))


def corpus(workdir: Path) -> None:
    sites_dir = workdir / "sites"
    sites_dir.mkdir(parents=True, exist_ok=True)
    archive = zipfile.ZipFile(io.BufferedReader(_HttpRange(DUMP_URL), 32 << 20))
    members = [
        (index, info)
        for index, info in enumerate(archive.infolist())
        if (parts := info.filename.rsplit(".", 2)) and len(parts) == 3
        if parts[1] in DUMP_LABELS and parts[2] == "txt"
    ]
    done = workdir / "fingerprints.jsonl"
    seen = set()
    if done.exists():
        seen = {json.loads(line)["member"] for line in done.open()}
    pending: queue.Queue[tuple[int, str, bytes | None] | BaseException | None] = (
        queue.Queue(maxsize=4)
    )

    def fetch() -> None:
        try:
            for index, info in members:
                if index not in seen:
                    label = info.filename.rsplit(".", 2)[1]
                    data: bytes | None = None
                    if info.file_size <= MAX_MEMBER_BYTES:
                        try:
                            data = archive.read(info)
                        except (zipfile.BadZipFile, zlib.error, EOFError):
                            data = b""  # a corrupt member is recorded as an error
                    pending.put((index, label, data))
        except BaseException as error:  # a failed download ends the pass
            pending.put(error)
        pending.put(None)

    threading.Thread(target=fetch, daemon=True).start()
    with done.open("a") as out:
        while (item := pending.get()) is not None:
            if isinstance(item, BaseException):
                raise SystemExit(f"Download failed ({item}); rerun to resume.")
            index, label, data = item
            record: dict[str, Any] = {"member": index, "label": label}
            if data is None:
                record["error"] = "too large for a chip export"
            else:
                try:
                    export = parse_raw_export(_member_text(data), f"member {index}")
                    record.update(_export_record(export))
                    _save_sites(sites_dir, export.fingerprint, _member_text(data))
                except (
                    WGSExtractError,
                    UnicodeDecodeError,
                    ValueError,
                    OSError,
                    EOFError,
                    zipfile.BadZipFile,
                    zlib.error,
                ) as e:
                    record["error"] = str(e).split(":")[0][:80]
            out.write(json.dumps(record, sort_keys=True) + "\n")
            out.flush()
            print(index, record.get("vendor"), record.get("markers"), flush=True)


def _export_record(export: RawExport) -> dict[str, Any]:
    fp = export.fingerprint
    return {
        "vendor": export.vendor,
        "chip": export.declared_chip,
        "build": export.declared_build,
        "month": export.export_month,
        "layout": export.layout,
        "markers": fp.markers,
        "ids_sha256": fp.ids_sha256,
        "sites_sha256": fp.sites_sha256,
        "sketch": encode_sketch(fp.sketch),
    }


def _save_sites(directory: Path, fp: Fingerprint, lines: TextIO) -> None:
    path = directory / f"{fp.sites_sha256}.tsv.gz"
    if path.exists():
        return
    rows = sorted(_rows(lines))
    tmp = path.with_suffix(".partial")
    with gzip.GzipFile(tmp, "wb", mtime=0) as out:
        out.write("".join(f"{m}\t{c}\t{p}\n" for m, c, p in rows).encode())
    tmp.rename(path)


def _rows(lines: TextIO) -> list[tuple[str, str, str]]:
    rows = []
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.replace('"', "").split("\t" if "\t" in line else ",")
        if fields[0].strip().lower() in _COLUMN_LABELS:
            continue
        rows.append(tuple(field.strip() for field in fields[:3]))
    return rows  # type: ignore[return-value]


def _template_fingerprint(name: str, roots: list[str]) -> Fingerprint | None:
    info = get_template_format(name)
    if info is None:
        return None
    try:
        _, bodies = _resolve_templates_for_format(roots, name, info)
    except WGSExtractError:
        return None
    return fingerprint(row for body in bodies for row in template_rows(name, body))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b)


def _load_sites(workdir: Path, digest: str) -> frozenset[str]:
    with gzip.open(workdir / "sites" / f"{digest}.tsv.gz", "rt") as stream:
        return frozenset(line.split("\t", 1)[0] for line in stream)


def build(workdir: Path, roots: list[str], spec_path: Path, output: Path) -> None:
    """Merge the template fingerprints with the corpus groups named in SPEC.

    SPEC (JSON) lists every catalogue entry: template entries name a template,
    corpus entries name the ``ids_sha256`` of an observed export group.
    """
    spec = json.loads(spec_path.read_text())
    records = [
        json.loads(line)
        for line in (workdir / "fingerprints.jsonl").open()
        if "error" not in line
    ]
    groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in records:
        groups[record["ids_sha256"]].append(record)

    templates: dict[str, tuple[Fingerprint, frozenset[str]]] = {}
    for name in spec["templates"]:
        fp = _template_fingerprint(name, roots)
        if fp is None:
            raise SystemExit(f"Template {name} is not installed under {roots}")
        templates[name] = (fp, fp.ids)

    entries = []
    for entry in spec["revisions"]:
        out = {k: v for k, v in entry.items() if k not in {"group", "template"}}
        if "group" in entry:
            members = groups.get(entry["group"], [])
            if len(members) < MIN_EXPORTS:
                raise SystemExit(f"{entry['id']}: fewer than {MIN_EXPORTS} exports")
            sites = collections.Counter(m["sites_sha256"] for m in members)
            site_hash, _ = sites.most_common(1)[0]
            builds = {m["build"] for m in members if m["sites_sha256"] == site_hash}
            ids = _load_sites(workdir, site_hash)
            out.update(
                markers=members[0]["markers"],
                ids_sha256=entry["group"],
                sites_sha256=site_hash,
                sketch=members[0]["sketch"],
                build=entry.get("build") or _one(builds, entry["id"]),
                source=DUMP_SOURCE,
            )
        else:
            fp, ids = templates[entry["template"]]
            members = groups.get(fp.ids_sha256, [])
            out.update(
                markers=fp.markers,
                ids_sha256=fp.ids_sha256,
                sites_sha256=fp.sites_sha256,
                sketch=encode_sketch(fp.sketch),
                source=entry.get("source")
                or f"raw_file_templates/{entry['template']} (see its README)",
            )
        months = sorted(m["month"] for m in members if m["month"])
        out.update(
            exports=len(members),
            period=f"{months[0]}/{months[-1]}" if months else None,
        )
        out["template"] = _closest_template(out, ids, templates, entry.get("template"))
        entries.append(out)
    catalogue = {
        "format": "wgsextract-chip-revisions",
        "version": 1,
        "sketch": "bottom-256 BLAKE2b-64 of distinct marker IDs, base64 big-endian",
        "corpus": {"source": DUMP_SOURCE, "url": DUMP_URL, "sha1": DUMP_SHA1},
        "revisions": entries,
    }
    output.write_text(json.dumps(catalogue, indent=1, sort_keys=False) + "\n")
    print(f"{output}: {len(entries)} revisions")


def _one(values: set[str | None], name: str) -> str:
    values.discard(None)
    if len(values) != 1:
        raise SystemExit(f"{name}: declared builds {values}; set 'build' in the spec")
    return next(iter(values))  # type: ignore[return-value]


def _closest_template(
    entry: dict[str, Any],
    ids: frozenset[str],
    templates: dict[str, tuple[Fingerprint, frozenset[str]]],
    own: str | None,
) -> dict[str, Any] | None:
    best: tuple[float, str] | None = None
    for name, (fp, template_ids) in templates.items():
        if fp.ids_sha256 == entry["ids_sha256"]:
            relation = "exact" if fp.sites_sha256 == entry["sites_sha256"] else None
            return _relation(name, relation or "same-markers", 0, 0)
        score = _jaccard(ids, template_ids)
        if best is None or score > best[0]:
            best = (score, name)
    if own or best is None or best[0] < 0.5:
        return None
    name = best[1]
    template_ids = templates[name][1]
    return _relation(name, "closest", len(ids - template_ids), len(template_ids - ids))


def _relation(name: str, relation: str, added: int, removed: int) -> dict[str, Any]:
    from wgsextract_cli.commands.microarray import _MICROARRAY_TEMPLATE_MAP

    keys = sorted(k for k, v in _MICROARRAY_TEMPLATE_MAP.items() if v == name)
    return {
        "name": name,
        "formats": keys[-1] if keys else name.lower(),
        "relation": relation,
        "added": added,
        "removed": removed,
    }


def survey(workdir: Path, roots: list[str], names: list[str]) -> None:
    """Print corpus groups with their nearest templates, to write the spec."""
    templates = {n: fp for n in names if (fp := _template_fingerprint(n, roots))}
    groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    errors = 0
    for line in (workdir / "fingerprints.jsonl").open():
        record = json.loads(line)
        if "error" in record:
            errors += 1
            continue
        groups[record["ids_sha256"]].append(record)
    print(f"errors: {errors}")
    for digest, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        first = members[0]
        sketch = re.sub(r"\s", "", first["sketch"])
        from wgsextract_cli.core.microarray_revisions import decode_sketch

        near = sorted(
            (
                (round(estimate_jaccard(decode_sketch(sketch), fp.sketch), 3), n)
                for n, fp in templates.items()
            ),
            reverse=True,
        )[:2]
        months = sorted(m["month"] for m in members if m["month"])
        print(
            json.dumps(
                {
                    "group": digest,
                    "n": len(members),
                    "markers": first["markers"],
                    "vendors": collections.Counter(m["vendor"] for m in members),
                    "labels": collections.Counter(m["label"] for m in members),
                    "chips": collections.Counter(m["chip"] for m in members),
                    "builds": collections.Counter(m["build"] for m in members),
                    "sites": len({m["sites_sha256"] for m in members}),
                    "period": f"{months[0]}/{months[-1]}" if months else None,
                    "near": near,
                }
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    corpus_cmd = sub.add_parser("corpus")
    corpus_cmd.add_argument("workdir", type=Path)
    for name in ("survey", "build"):
        cmd = sub.add_parser(name)
        cmd.add_argument("workdir", type=Path)
        cmd.add_argument("--templates", action="append", default=[])
        if name == "build":
            cmd.add_argument("--spec", type=Path, required=True)
            cmd.add_argument("--output", type=Path, default=CATALOGUE)
    args = parser.parse_args()
    packaged = str(CATALOGUE.parents[1] / "raw_file_templates")
    if args.cmd == "corpus":
        corpus(args.workdir)
    elif args.cmd == "survey":
        from wgsextract_cli.commands.benchmark.chip import CONSUMER_FORMATS

        survey(args.workdir, [*args.templates, packaged], list(CONSUMER_FORMATS))
    else:
        build(args.workdir, [*args.templates, packaged], args.spec, args.output)


if __name__ == "__main__":
    main()
