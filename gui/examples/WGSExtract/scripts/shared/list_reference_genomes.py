#!/usr/bin/env python3
"""Library list provider for the WGSExtract reference-genome control.

Emits a JSON document of `{options, items}` describing the user's
local reference-genome library, derived by joining:

  1. the upstream wgsextract-cli seed catalog (`seed_genomes.csv`,
     authored and maintained inside that project), and
  2. the on-disk state of `<library>/genomes/<final>` (+ `.fai`,
     `.partial`, etc).

Only catalog entries whose final file exists locally make it into the
`options` list; `items` is the full catalog annotated with status and
on-disk size for the library page table.

The catalog is the one shipped with the CLI installed by setup at
`<bundle>/runtime/wgsextract-cli/app/src/wgsextract_cli/assets/reference/`.
Without it the script fails, so the Library page reports a missing runtime
instead of showing a stale catalog.

Usage:
  list-reference-genomes.py [all|options|items] [<library>]   (scripts/posix, scripts/windows)

The library directory defaults to $GUI_FOR_CLI_BUNDLE_WORKSPACE/reference,
else $PWD/reference.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from concurrent.futures import CancelledError, TimeoutError as FuturesTimeoutError
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

REMOTE_SIZE_CACHE_FILE = ".remote-sizes.json"
REMOTE_SIZE_TTL_SECONDS = 30 * 24 * 3600  # 30 days
REMOTE_SIZE_HEAD_TIMEOUT = 4.0
REMOTE_SIZE_BUDGET_SECONDS = 8.0
REMOTE_SIZE_MAX_WORKERS = 8


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


CATALOG_PATH = Path("runtime/wgsextract-cli/app/src/wgsextract_cli/assets/reference/seed_genomes.csv")


def find_catalog() -> Path:
    catalog = Path(__file__).resolve().parents[2] / CATALOG_PATH
    if not catalog.is_file():
        sys.exit(
            f"WGS Extract reference catalog not found at {catalog}. "
            "Run WGS Extract setup to install the CLI runtime."
        )
    return catalog


def status_for(genome_file: Path) -> str:
    if genome_file.is_file() and genome_file.with_suffix(genome_file.suffix + ".fai").is_file():
        return "installed"
    if genome_file.is_file():
        return "unindexed"
    if Path(str(genome_file) + ".partial").is_file():
        return "incomplete"
    return "missing"


def size_label(genome_file: Path) -> str:
    total = 0
    for ext in ("", ".partial", ".fai", ".gzi", ".dict"):
        candidate = Path(str(genome_file) + ext) if ext else genome_file
        if candidate.is_file():
            total += candidate.stat().st_size
    if total == 0:
        return ""
    return human_bytes(total)


def human_bytes(total: int) -> str:
    if total <= 0:
        return ""
    if total > 1_073_741_824:
        return f"{total / 1_073_741_824:.1f} GB"
    if total > 1_048_576:
        return f"{total / 1_048_576:.1f} MB"
    return f"{total / 1024:.1f} KB"


def load_size_cache(library: Path) -> dict:
    cache_file = library / REMOTE_SIZE_CACHE_FILE
    if not cache_file.is_file():
        return {}
    try:
        with cache_file.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_size_cache(library: Path, cache: dict) -> None:
    try:
        library.mkdir(parents=True, exist_ok=True)
        cache_file = library / REMOTE_SIZE_CACHE_FILE
        tmp = cache_file.with_suffix(cache_file.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(cache, handle, separators=(",", ":"))
        tmp.replace(cache_file)
    except OSError:
        pass


def fetch_remote_size(url: str) -> int | None:
    if urlparse(url).scheme not in {"http", "https"}:
        return None
    try:
        request = Request(url, method="HEAD", headers={"User-Agent": "gui-for-cli/list-reference-genomes"})
        with urlopen(request, timeout=REMOTE_SIZE_HEAD_TIMEOUT) as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit():
                return int(length)
    except (URLError, ValueError, TimeoutError, OSError):
        return None
    return None


def populate_remote_sizes(records: list[dict], cache: dict) -> dict:
    """Fill `cache` (mutated) with `{url: {bytes, fetched_at}}` for any
    catalog URL whose entry is missing or stale. Operates within an
    overall wall-clock budget so a slow network can't stall the page."""
    now = time.time()
    pending: list[tuple[str, str]] = []  # (final, url)
    for record in records:
        url = record.get("url") or ""
        if not url:
            continue
        entry = cache.get(url)
        if entry and isinstance(entry, dict):
            fetched = entry.get("fetched_at", 0)
            if isinstance(fetched, (int, float)) and now - fetched < REMOTE_SIZE_TTL_SECONDS:
                continue
        pending.append((record["final"], url))

    if not pending or env("GUI_FOR_CLI_OFFLINE") == "1":
        return cache

    deadline = now + REMOTE_SIZE_BUDGET_SECONDS
    with ThreadPoolExecutor(max_workers=REMOTE_SIZE_MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_remote_size, url): url for _, url in pending}
        try:
            completed = as_completed(futures, timeout=REMOTE_SIZE_BUDGET_SECONDS)
            for future in completed:
                remaining = deadline - time.time()
                if remaining <= 0:
                    continue
                url = futures[future]
                try:
                    size = future.result(timeout=max(0.1, remaining))
                except (CancelledError, FuturesTimeoutError, OSError, URLError):
                    size = None
                if size and size > 0:
                    cache[url] = {"bytes": size, "fetched_at": time.time()}
        except FuturesTimeoutError:
            for future in futures:
                future.cancel()
    return cache


def build_for(code: str, final: str, description: str) -> str:
    blob = f"{code} {final} {description}"
    rules = [
        (("GRCh37", "hg19", "hs37", "hg37"), "GRCh37 / hg19"),
        (("GRCh38", "hg38", "hs38"), "GRCh38 / hg38"),
        (("T2T", "CHM13", "chm13", "HG002"), "T2T / CHM13"),
        (("Dog", "Canis", "GSD"), "Dog"),
        (("Cat", "Felis", "Fca"), "Cat"),
    ]
    for tokens, label in rules:
        if any(token in blob for token in tokens):
            return label
    return final


def tags_for(label: str) -> list[dict]:
    if "(Rec)" in label:
        return [{"id": "recommended", "title": "Recommended", "style": "primary"}]
    return []


def clean_label(label: str, source: str) -> str:
    primary = source[:-4] if source.endswith("-Alt") else source
    for token in ("(Rec)", f"({source})", f"({primary})"):
        if not token or token == "()":
            continue
        label = label.replace(token, "")
    while "  " in label:
        label = label.replace("  ", " ")
    return label.strip()


def load_records(catalog: Path) -> Iterable[dict]:
    with catalog.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            code = (row.get("Pyth Code") or "").strip()
            source = (row.get("Source") or "").strip()
            final = (row.get("Final File Name") or "").strip()
            label = (row.get("Library Menu Label") or "").strip()
            description = (row.get("Description") or "").strip()
            url = (row.get("URL") or "").strip()
            if not code or not source or not final or not label:
                continue
            yield {
                "code": code, "source": source, "final": final,
                "label": label, "description": description, "url": url,
            }


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "all"
    library_arg = argv[2] if len(argv) > 2 else ""
    library = library_arg or os.path.join(env("GUI_FOR_CLI_BUNDLE_WORKSPACE", os.getcwd()), "reference")
    genomes_dir = Path(library) / "genomes"
    records = list(load_records(find_catalog()))

    output: dict = {}

    if mode in ("all", "options"):
        options: list[dict] = []
        seen_finals: set[str] = set()
        selected_assigned = False
        for record in records:
            final = record["final"]
            if final in seen_finals:
                continue
            seen_finals.add(final)
            genome_file = genomes_dir / final
            status = status_for(genome_file)
            if status not in ("installed", "unindexed"):
                continue
            entry = {"id": str(genome_file), "title": record["label"], "status": status}
            if not selected_assigned:
                entry["selected"] = True
                selected_assigned = True
            options.append(entry)
        output["options"] = options

    if mode in ("all", "items"):
        size_cache = load_size_cache(Path(library))
        before = json.dumps(size_cache, sort_keys=True)
        populate_remote_sizes(records, size_cache)
        if json.dumps(size_cache, sort_keys=True) != before:
            save_size_cache(Path(library), size_cache)

        items: list[dict] = []
        for record in records:
            final = record["final"]
            genome_file = genomes_dir / final
            status = status_for(genome_file)
            display_label = clean_label(record["label"], record["source"])
            row_id = f"{record['code']}-{record['source']}-{final}"
            local_label = size_label(genome_file)
            if local_label:
                size_value = local_label
            else:
                cached = size_cache.get(record.get("url") or "")
                cached_bytes = cached.get("bytes") if isinstance(cached, dict) else None
                size_value = human_bytes(cached_bytes) if isinstance(cached_bytes, int) else ""
            items.append({
                "id": row_id,
                "title": display_label,
                "status": status,
                "tags": tags_for(record["label"]),
                "tooltip": record["description"],
                "values": {
                    "name": display_label,
                    "build": build_for(record["code"], final, record["description"]),
                    "source": record["source"],
                    "code": record["code"],
                    "final": final,
                    "ref": str(genome_file),
                    "size": size_value,
                    "description": record["description"],
                },
            })
        output["items"] = items

    json.dump(output, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
