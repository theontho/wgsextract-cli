"""Explicit, pinned GRCh38 yBrowse installation; no runtime downloads."""

import contextlib
import gzip
import hashlib
import json
import re
import tempfile
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO, cast

from wgsextract_cli.core.utils import WGSExtractError, run_command

SOURCE_URL = "https://ybrowse.org/gbrowse2/gff/snps_hg38.vcf.gz"
SOURCE_SHA256 = "b7984349c575c3d582933993a92770d6143603d3b895d3a0612340b167f37b62"
COPYRIGHT = "CC-BY-SA Thomas Krahn YSEQ (2022)."
Y_LENGTH = 57_227_415
_FILES = ("sites.vcf.gz", "sites.vcf.gz.tbi", "targets.tsv.gz", "targets.tsv.gz.tbi")
_BASES = set("ACGT")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def _text(path: Path) -> Iterator[TextIO]:
    with path.open("rb") as stream:
        compressed = stream.read(2) == b"\x1f\x8b"
    opener = gzip.open if compressed else open
    with opener(path, "rt", encoding="utf-8") as text:
        yield text


def _validate_header(lines: list[str]) -> None:
    if not any(line.startswith("##fileformat=VCFv4.") for line in lines):
        raise WGSExtractError("yBrowse snapshot is not a VCF.")
    if f'##copyright="{COPYRIGHT}"' not in lines:
        raise WGSExtractError(
            f"yBrowse snapshot must retain the GRCh38 copyright: {COPYRIGHT}"
        )
    contigs = [
        line
        for line in lines
        if line.startswith("##contig=") and re.search(r"\bID=(?:chrY|Y)(?:,|>)", line)
    ]
    if len(contigs) != 1 or not re.search(rf"\blength={Y_LENGTH}(?:,|>)", contigs[0]):
        raise WGSExtractError(
            f"yBrowse requires GRCh38 chrY length {Y_LENGTH}; GRCh37/T2T are unsupported."
        )
    for tag in ("HG", "ISOGG"):
        if not any(
            line.startswith(f"##INFO=<ID={tag},") and ",Type=String," in line
            for line in lines
        ):
            raise WGSExtractError(f"yBrowse snapshot lacks the INFO/{tag} definition.")


def _write_group(
    pos: int,
    group: dict[tuple[str, str], tuple[set[str], set[str], set[str]]],
    sites: TextIO,
    targets: TextIO,
) -> int:
    if not group:
        return 0
    refs = {ref for ref, _ in group}
    if len(refs) != 1:
        raise WGSExtractError(f"Conflicting yBrowse REF alleles at chrY:{pos}.")
    ref = next(iter(refs))
    alts = sorted(alt for _, alt in group)
    targets.write(f"chrY\t{pos}\t{','.join([ref, *alts])}\n")
    for alt in alts:
        names, hgs, isoggs = group[(ref, alt)]
        info = ";".join(
            f"{tag}={','.join(sorted(values))}"
            for tag, values in (("HG", hgs), ("ISOGG", isoggs))
            if values
        )
        sites.write(
            f"chrY\t{pos}\t{';'.join(sorted(names)) or '.'}\t{ref}\t{alt}"
            f"\t.\t.\t{info or '.'}\n"
        )
    return len(group)


def prepare_snapshot(snapshot: Path, directory: Path) -> dict[str, object]:
    """Stream sorted SNPs, aggregating aliases only for identical REF/ALT."""
    sites_path = directory / "sites.vcf"
    targets_path = directory / "targets.tsv"
    header: list[str] = []
    records = alleles = skipped = 0
    position = 0
    group: dict[tuple[str, str], tuple[set[str], set[str], set[str]]] = {}
    try:
        with (
            _text(snapshot) as source,
            sites_path.open("w", encoding="utf-8", newline="\n") as sites,
            targets_path.open("w", encoding="utf-8", newline="\n") as targets,
        ):
            for line in source:
                if line.startswith("##"):
                    header.append(line.rstrip("\r\n"))
                    continue
                if line.startswith("#CHROM\t"):
                    _validate_header(header)
                    sites.write(
                        "##fileformat=VCFv4.2\n"
                        f'##copyright="{COPYRIGHT}"\n'
                        f"##contig=<ID=chrY,length={Y_LENGTH}>\n"
                        '##INFO=<ID=HG,Number=.,Type=String,Description="yBrowse haplogroups'
                        ' for this ALT allele, not a sample haplogroup prediction">\n'
                        '##INFO=<ID=ISOGG,Number=.,Type=String,Description="yBrowse ISOGG'
                        ' haplogroups for this ALT allele">\n'
                        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
                    )
                    break
            else:
                raise WGSExtractError("yBrowse snapshot lacks its #CHROM header.")
            for line in source:
                cols = line.rstrip("\r\n").split("\t")
                if len(cols) < 8 or cols[0] not in ("chrY", "Y"):
                    raise WGSExtractError(
                        "yBrowse snapshot must contain only Y records."
                    )
                pos = int(cols[1])
                if not position <= pos <= Y_LENGTH or pos < 1:
                    raise WGSExtractError(
                        "yBrowse positions are unsorted/outside GRCh38 Y."
                    )
                if pos != position:
                    alleles += _write_group(position, group, sites, targets)
                    group = {}
                    position = pos
                records += 1
                if records > 10_000_000:
                    raise WGSExtractError(
                        "yBrowse snapshot exceeds the phase-1 record limit."
                    )
                ref = cols[3].upper()
                alts = [a.upper() for a in cols[4].split(",")]
                valid = [a for a in alts if a in _BASES and ref in _BASES and a != ref]
                if not valid:
                    skipped += 1
                    continue
                info = dict(
                    item.split("=", 1) for item in cols[7].split(";") if "=" in item
                )
                for alt in valid:
                    names, hgs, isoggs = group.setdefault(
                        (ref, alt), (set(), set(), set())
                    )
                    names.update(n for n in cols[2].split(";") if n != ".")
                    hgs.update(
                        n for n in info.get("HG", "").split(",") if n not in ("", ".")
                    )
                    isoggs.update(
                        n
                        for n in info.get("ISOGG", "").split(",")
                        if n not in ("", ".")
                    )
            alleles += _write_group(position, group, sites, targets)
        if not alleles:
            raise WGSExtractError("yBrowse snapshot has no usable SNP alleles.")
    except (OSError, EOFError, UnicodeError, ValueError) as exc:
        raise WGSExtractError(f"Invalid yBrowse snapshot: {exc}") from exc
    for plain, index_args in (
        (sites_path, ["-p", "vcf"]),
        (targets_path, ["-s", "1", "-b", "2", "-e", "2"]),
    ):
        compressed = Path(str(plain) + ".gz")
        with compressed.open("wb") as out:
            run_command(["bgzip", "-c", str(plain)], stdout=out)
        run_command(["tabix", *index_args, str(compressed)])
        plain.unlink()
    return {
        "schema": 1,
        "assembly": "GRCh38",
        "chrY_length": Y_LENGTH,
        "copyright": COPYRIGHT,
        "attribution": f"Thomas Krahn / YSEQ; {SOURCE_URL}",
        "source_url": SOURCE_URL,
        "source_sha256": file_sha256(snapshot),
        "source_records": records,
        "snp_alleles": alleles,
        "skipped_non_snp_records": skipped,
        "sites_only": True,
        "files": {name: file_sha256(directory / name) for name in _FILES},
    }


def _download(path: Path) -> None:
    try:
        with (
            urllib.request.urlopen(SOURCE_URL, timeout=60) as response,
            path.open("wb") as out,
        ):
            total = 0
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                total += len(chunk)
                if total > 256 * 1024 * 1024:
                    raise WGSExtractError(
                        "yBrowse download exceeds the pinned snapshot limit."
                    )
                out.write(chunk)
    except OSError as exc:
        raise WGSExtractError(f"yBrowse download failed: {exc}") from exc
    actual = file_sha256(path)
    if actual != SOURCE_SHA256:
        raise WGSExtractError(
            f"yBrowse snapshot hash mismatch: expected {SOURCE_SHA256}, got {actual}. "
            "Upstream changed; no resource was installed. Supply a header-verified "
            "local GRCh38 snapshot with ref ybrowse --install --snapshot PATH."
        )


def install_snapshot(library: Path, snapshot: Path | None = None) -> Path:
    root = library / "ybrowse" / "hg38"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".install_", dir=root) as temporary:
        stage = Path(temporary)
        source = snapshot
        if source is None:
            source = stage / "download.vcf.gz"
            _download(source)
        elif not source.is_file():
            raise WGSExtractError(f"yBrowse snapshot not found: {source}")
        resource = stage / "resource"
        resource.mkdir()
        manifest = prepare_snapshot(source, resource)
        manifest["source_kind"] = "user-supplied" if snapshot else "pinned-download"
        (resource / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        version = str(manifest["source_sha256"])
        destination = root / version
        if not destination.exists():
            resource.rename(destination)
        else:
            try:
                _validate_resource(destination)
            except WGSExtractError:
                previous = stage / "previous"
                destination.rename(previous)
                try:
                    resource.rename(destination)
                except OSError:
                    previous.rename(destination)
                    raise
        pointer = stage / "current.json"
        pointer.write_text(
            json.dumps({"version": version}), encoding="utf-8", newline="\n"
        )
        pointer.replace(root / "current.json")
    return destination


def _validate_resource(directory: Path) -> dict[str, object]:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if (
            manifest["schema"] != 1
            or manifest["assembly"] != "GRCh38"
            or manifest["chrY_length"] != Y_LENGTH
            or manifest["copyright"] != COPYRIGHT
            or manifest["attribution"] != f"Thomas Krahn / YSEQ; {SOURCE_URL}"
            or manifest["source_url"] != SOURCE_URL
            or manifest["sites_only"] is not True
            or not re.fullmatch(r"[a-f0-9]{64}", manifest["source_sha256"])
        ):
            raise ValueError("unsupported manifest")
        if any(
            file_sha256(directory / name) != manifest["files"][name] for name in _FILES
        ):
            raise ValueError("resource checksum mismatch")
        return cast(dict[str, object], manifest)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WGSExtractError(
            f"Invalid yBrowse resource in {directory}: {exc}. Reinstall explicitly."
        ) from exc


def installed_snapshot(library: Path) -> tuple[Path, dict[str, object]]:
    root = library / "ybrowse" / "hg38"
    try:
        version = json.loads((root / "current.json").read_text(encoding="utf-8"))[
            "version"
        ]
        if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version):
            raise ValueError("invalid snapshot version")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WGSExtractError(
            "No verified GRCh38 yBrowse resource. Install explicitly with "
            "wgsextract ref ybrowse --install [--snapshot PATH] --ref LIBRARY."
        ) from exc
    directory = root / version
    return directory, _validate_resource(directory)
