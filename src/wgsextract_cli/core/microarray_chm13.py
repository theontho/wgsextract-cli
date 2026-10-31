"""Reference signatures and sex-aware ploidy for the additive CHM13 path."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from wgsextract_cli.core.microarray_site_map import (
    _CHROMS,
    SiteMap,
    load_site_map,
)
from wgsextract_cli.core.utils import WGSExtractError, popen


@dataclass(frozen=True)
class ReferenceContig:
    length: int
    md5: str


@dataclass(frozen=True)
class PARInterval:
    chrom: str
    start0: int
    end0: int


@dataclass(frozen=True)
class CHM13Catalogue:
    mapping: SiteMap
    contigs: Mapping[str, ReferenceContig]
    source_lengths: Mapping[str, int]
    par: tuple[PARInterval, ...]
    fasta_sha256: str
    fai_sha256: str
    gzi_sha256: str
    non_snv_sites: frozenset[tuple[str, int]] = frozenset()
    non_snv_ids: frozenset[str] = frozenset()


def _digest(value: object, size: int) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(rf"[a-f0-9]{{{size}}}", value) is None
    ):
        raise ValueError("Invalid reference digest")
    return value


def _coordinate(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 0xFFFFFFFF:
        raise ValueError("Invalid reference coordinate")
    return value


def load_chm13_catalogue(manifest_path: Path | None = None) -> CHM13Catalogue:
    if manifest_path is None:
        manifest_path = (
            Path(__file__).resolve().parents[1]
            / "assets/microarray/chm13/v1/manifest.json"
        )
    try:
        with manifest_path.open("rb") as source:
            data = source.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Manifest exceeds the size limit")
        with Path(str(manifest_path) + ".sha256").open("rb") as checksum:
            expected = _digest(checksum.read(66).decode("ascii").strip(), 64)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Reference manifest SHA256 mismatch")
        manifest = json.loads(data)
        if (
            _coordinate(manifest["compressed_bytes"], minimum=1) > 7 * 1024 * 1024
            or _coordinate(manifest["uncompressed_bytes"], minimum=1) > 17 * 1024 * 1024
            or sum(_coordinate(row["rows"]) for row in manifest["source_contigs"])
            > 3_000_000
        ):
            raise ValueError("CHM13 v1 asset exceeds its measured size/row budget")
        profile = manifest["provenance"]["native_reference"]
        bridge = manifest["provenance"]["mitochondria"]
        filename = bridge["alignment_file"]
        if (
            not isinstance(filename, str)
            or re.fullmatch(r"mt-alignment-v1-[a-f0-9]{16}\.fa\.gz", filename) is None
        ):
            raise ValueError("Invalid pinned mitochondrial alignment filename")
        with (manifest_path.parent / filename).open("rb") as alignment:
            compressed = alignment.read(64 * 1024 + 1)
        if len(compressed) > 64 * 1024 or hashlib.sha256(
            compressed
        ).hexdigest() != _digest(bridge["alignment_sha256"], 64):
            raise ValueError("Pinned mitochondrial alignment SHA256 mismatch")
        decoder = zlib.decompressobj(31)
        decoded = decoder.decompress(compressed, 128 * 1024 + 1)
        if (
            len(decoded) > 128 * 1024
            or not decoder.eof
            or decoder.unused_data
            or hashlib.sha256(decoded).hexdigest()
            != _digest(bridge["alignment_uncompressed_sha256"], 64)
        ):
            raise ValueError("Invalid pinned mitochondrial alignment payload")
        if profile["chrY_PAR_masking"] != "masked":
            raise ValueError("Unsupported chrY PAR masking state")
        contigs: dict[str, ReferenceContig] = {}
        for row in profile["contigs"]:
            chrom = row["name"]
            if chrom not in _CHROMS or chrom in contigs:
                raise ValueError("Invalid or duplicate reference chromosome")
            contigs[chrom] = ReferenceContig(
                _coordinate(row["length"], minimum=1), _digest(row["md5"], 32)
            )
        if set(contigs) != set(_CHROMS):
            raise ValueError("CHM13 reference profile needs all 25 primary chromosomes")
        if contigs["chrM"].md5 != "ec493a132ac4823aa696e37109f64972":
            raise ValueError("Unsupported CHM13 mitochondrial sequence")
        par = tuple(
            PARInterval(
                row["chrom"], _coordinate(row["start0"]), _coordinate(row["end0"])
            )
            for row in profile["par_intervals"]
        )
        if len(par) != 4:
            raise ValueError("Expected two PAR intervals on each of chrX and chrY")
        for chrom in ("chrX", "chrY"):
            intervals = sorted(
                (interval for interval in par if interval.chrom == chrom),
                key=lambda interval: interval.start0,
            )
            if (
                len(intervals) != 2
                or intervals[0].start0 != 0
                or not intervals[0].end0 < intervals[1].start0
                or intervals[1].end0 != contigs[chrom].length
                or any(interval.start0 >= interval.end0 for interval in intervals)
            ):
                raise ValueError(f"Invalid CHM13 PAR intervals for {chrom}")
        targets = {row["name"]: row["length"] for row in manifest["target_contigs"]}
        if targets != {name: contig.length for name, contig in contigs.items()}:
            raise ValueError("Reference profile and site-map contigs disagree")
        mapping = load_site_map(manifest_path, manifest_data=data)
        marker_metadata = manifest["provenance"].get("markers", {})
        if not isinstance(marker_metadata, dict):
            raise ValueError("Invalid source marker metadata")
        alleles = marker_metadata.get("source_alleles", {})
        if not isinstance(alleles, dict):
            raise ValueError("Invalid source allele metadata")
        rows = alleles.get("non_snv_sites", [])
        ids = alleles.get("non_snv_ids", [])
        if (
            not isinstance(rows, list)
            or any(
                not isinstance(row, list)
                or len(row) != 2
                or not isinstance(row[0], str)
                for row in rows
            )
            or not isinstance(ids, list)
            or any(not isinstance(marker, str) or not marker for marker in ids)
        ):
            raise ValueError("Invalid source non-SNV marker exclusions")
        non_snv_sites = frozenset(
            (row[0], _coordinate(row[1], minimum=1)) for row in rows
        )
        non_snv_ids = frozenset(ids)
        if (
            len(non_snv_sites) != alleles.get("non_snv_site_count", 0)
            or len(non_snv_ids) != alleles.get("non_snv_id_count", 0)
            or any(mapping.source_lookup(*site) is None for site in non_snv_sites)
        ):
            raise ValueError("Invalid source non-SNV marker exclusions")
        return CHM13Catalogue(
            mapping,
            MappingProxyType(contigs),
            MappingProxyType(
                {row["name"]: row["length"] for row in manifest["source_contigs"]}
            ),
            par,
            _digest(profile["sha256"], 64),
            _digest(profile["fai_sha256"], 64),
            _digest(profile["gzi_sha256"], 64),
            non_snv_sites,
            non_snv_ids,
        )
    except (OSError, ValueError, TypeError, KeyError, zlib.error) as error:
        raise WGSExtractError(f"Invalid CHM13 reference profile: {error}") from error


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_chm13_reference(fasta: Path, catalogue: CHM13Catalogue) -> dict[str, str]:
    """Verify actual indexed sequences, using exact-file hashes as a fast path."""
    fai = Path(str(fasta) + ".fai")
    try:
        names: dict[str, str] = {}
        for line in fai.read_text().splitlines():
            fields = line.split("\t")
            actual, length_text = fields[:2]
            suffix = actual.removeprefix("chr")
            chrom = "chrM" if suffix in {"M", "MT"} else f"chr{suffix}"
            if chrom not in catalogue.contigs or chrom in names:
                raise ValueError(f"Unexpected or duplicate reference contig: {actual}")
            if int(length_text) != catalogue.contigs[chrom].length:
                raise ValueError(f"Reference length mismatch: {actual}")
            names[chrom] = actual
        if set(names) != set(catalogue.contigs):
            raise ValueError("Reference is missing primary chromosomes")
        gzi = Path(str(fasta) + ".gzi")
        if (
            _file_sha256(fasta) == catalogue.fasta_sha256
            and _file_sha256(fai) == catalogue.fai_sha256
            and gzi.is_file()
            and _file_sha256(gzi) == catalogue.gzi_sha256
        ):
            return names
        for chrom in _CHROMS:
            command = ["samtools", "faidx", str(fasta), names[chrom]]
            digest = hashlib.md5()
            length = 0
            with popen(command, stdout=subprocess.PIPE) as process:
                assert process.stdout is not None
                header = process.stdout.readline().strip()
                for sequence_line in process.stdout:
                    bases = sequence_line.strip().upper()
                    digest.update(bases)
                    length += len(bases)
                returncode = process.wait()
                if returncode:
                    raise subprocess.CalledProcessError(returncode, command)
            if header != f">{names[chrom]}".encode() or (
                length != catalogue.contigs[chrom].length
                or digest.hexdigest() != catalogue.contigs[chrom].md5
            ):
                raise ValueError(
                    f"CHM13 sequence mismatch: {names[chrom]} "
                    "(including chrY PAR masking / original chrM)"
                )
        return names
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
        raise WGSExtractError(f"CHM13 reference validation failed: {error}") from error


def write_chm13_ploidy(
    catalogue: CHM13Catalogue, names: Mapping[str, str], directory: Path
) -> Path:
    path = directory / "chm13.ploidy"
    with path.open("w", encoding="ascii", newline="\n") as destination:
        for chrom in _CHROMS:
            length = catalogue.contigs[chrom].length
            actual = names[chrom]
            if chrom in {"chrX", "chrY"}:
                destination.write(
                    f"{actual}\t1\t{length}\tF\t{2 if chrom == 'chrX' else 0}\n"
                )
                cursor = 0
                intervals = sorted(
                    (interval for interval in catalogue.par if interval.chrom == chrom),
                    key=lambda interval: interval.start0,
                )
                for interval in intervals:
                    if cursor < interval.start0:
                        destination.write(
                            f"{actual}\t{cursor + 1}\t{interval.start0}\tM\t1\n"
                        )
                    destination.write(
                        f"{actual}\t{interval.start0 + 1}\t{interval.end0}\tM\t"
                        f"{2 if chrom == 'chrX' else 0}\n"
                    )
                    cursor = interval.end0
                if cursor < length:
                    destination.write(f"{actual}\t{cursor + 1}\t{length}\tM\t1\n")
            else:
                ploidy = 1 if chrom == "chrM" else 2
                for sex in ("M", "F"):
                    destination.write(f"{actual}\t1\t{length}\t{sex}\t{ploidy}\n")
    return path


def validate_chm13_alignment_header(
    header: str, catalogue: CHM13Catalogue, names: Mapping[str, str]
) -> list[str]:
    seen: set[str] = set()
    actual_names: list[str] = []
    try:
        for line in header.splitlines():
            if not line.startswith("@SQ\t"):
                continue
            fields = dict(field.split(":", 1) for field in line.split("\t")[1:])
            actual = fields["SN"]
            suffix = actual.removeprefix("chr")
            chrom = "chrM" if suffix in {"M", "MT"} else f"chr{suffix}"
            if chrom not in catalogue.contigs:
                continue
            expected = catalogue.contigs[chrom]
            if (
                chrom in seen
                or actual != names[chrom]
                or int(fields["LN"]) != expected.length
                or fields.get("M5", "").lower() != expected.md5
            ):
                raise ValueError(f"SQ length/name/M5 mismatch: {actual}")
            seen.add(chrom)
            actual_names.append(actual)
        if seen != set(catalogue.contigs):
            raise ValueError("Missing primary chromosome SQ signatures")
    except (ValueError, KeyError) as error:
        raise WGSExtractError(
            "Cannot establish the CHM13 alignment reference variant: "
            f"{error}. Matching primary SQ M5 signatures are required, "
            "including original chrM and masked chrY PARs."
        ) from error
    return actual_names
