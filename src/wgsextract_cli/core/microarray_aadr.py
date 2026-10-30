"""Pinned AADR marker panels and allele-aware PLINK/EIGENSTRAT export."""

import contextlib
import gzip
import logging
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pyliftover import LiftOver

from wgsextract_cli.core.builds import canonical_build
from wgsextract_cli.core.microarray_targets import (
    HG19_TO_HG38_CHAIN_URL,
    _write_target,
)
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.utils import WGSExtractError, run_command

AADR_FORMATS = {
    "aadr_1240k": "1240k",
    "aadr_ho": "ho",
    "aadr_1240k+ho": "1240k_ho",
    "aadr_1240k_ho": "1240k_ho",
}
_MARKERS = Path(__file__).resolve().parents[1] / "assets/microarray/aadr/v66.p1.tsv.gz"
_COMPLEMENT = str.maketrans("ACGT", "TGCA")
_BED_CODE = {"2": 0, "1": 2, "0": 3, "9": 1}
_PRIMARY_CHROMS = {*(f"chr{i}" for i in range(1, 23)), "chrX", "chrY"}


@dataclass(frozen=True)
class AADRMarker:
    snp_id: str
    chrom: str
    genetic_position: str
    position: int
    ref: str
    alt: str
    ho: bool


def aadr_panel(format_key: str) -> str | None:
    return AADR_FORMATS.get(format_key.strip().lower().replace("-", "_"))


def aadr_markers(panel: str) -> Iterator[AADRMarker]:
    if panel not in {"1240k", "ho", "1240k_ho"}:
        raise ValueError(f"Unknown AADR panel: {panel}")
    with gzip.open(_MARKERS, "rt") as source:
        for line in source:
            if line.startswith("#"):
                continue
            snp_id, chrom, genetic, pos, ref, alt, ho = line.split()
            if panel != "ho" or ho == "1":
                yield AADRMarker(snp_id, chrom, genetic, int(pos), ref, alt, ho == "1")


def validate_aadr_sample_id(sample_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,38}", sample_id):
        raise WGSExtractError(
            "--aadr-sample-id must be 1-39 ASCII letters, digits, dots, "
            "underscores or hyphens, starting with a letter or digit."
        )


def _chrom_name(chrom: str) -> str:
    chrom = chrom.removeprefix("chr")
    return {"23": "X", "24": "Y", "90": "MT", "M": "MT"}.get(chrom, chrom)


def aadr_lifter(
    build: str | None, outdir: str, reflib_dir: str | None = None
) -> LiftOver | None:
    build = canonical_build(build or "")
    if build == "hg19":
        return None
    if build != "hg38":
        raise WGSExtractError("AADR export requires a known GRCh37 or GRCh38 build.")
    if reflib_dir:
        installed = Path(reflib_dir, "hg19ToHg38.over.chain.gz")
        if installed.is_file():
            return LiftOver(str(installed))
    chain = Path(outdir, "hg19ToHg38.over.chain.gz")
    if not chain.is_file() and not download_file(HG19_TO_HG38_CHAIN_URL, str(chain)):
        raise WGSExtractError("Could not download the GRCh37-to-GRCh38 AADR chain.")
    return LiftOver(str(chain))


def _marker_coordinate(
    marker: AADRMarker, lifter: LiftOver | None
) -> tuple[str, int, bool] | None:
    chrom = _chrom_name(marker.chrom)
    if lifter is None:
        return chrom, marker.position, False
    hits = lifter.convert_coordinate("chr" + chrom, marker.position - 1)
    # Never choose arbitrarily between multiple placements or an alternate contig.
    if not hits or len(hits) != 1 or hits[0][0] not in _PRIMARY_CHROMS:
        return None
    return _chrom_name(hits[0][0]), hits[0][1] + 1, hits[0][2] == "-"


def build_aadr_target(
    panels: list[str],
    outdir: str,
    lifter: LiftOver | None,
    base_target: str | None = None,
) -> str:
    """Use a separate run-local target; never change legacy cached targets."""
    markers: dict[tuple[str, int], str] = {}
    if base_target:
        opener = gzip.open if base_target.endswith((".gz", ".bgz")) else open
        with opener(base_target, "rt") as source:
            for line in source:
                if line.startswith("#") or not line.strip():
                    continue
                chrom, position, snp_id = line.split()[:3]
                normalized = _chrom_name(chrom)
                markers[
                    (
                        "chrM" if normalized == "MT" else "chr" + normalized,
                        int(position),
                    )
                ] = snp_id
    panel = "ho" if set(panels) == {"ho"} else "1240k"
    for marker in aadr_markers(panel):
        coordinate = _marker_coordinate(marker, lifter)
        if coordinate:
            chrom, pos, _inverted = coordinate
            markers.setdefault(("chr" + chrom, pos), marker.snp_id)
    fd, target = tempfile.mkstemp(prefix="AADR_targets_", suffix=".tab.gz", dir=outdir)
    os.close(fd)
    try:
        _write_target(markers, target)
    except BaseException:
        Path(target).unlink(missing_ok=True)
        Path(target + ".tbi").unlink(missing_ok=True)
        raise
    return target


def aadr_vcf_nocalls(vcf: str, outdir: str) -> set[tuple[str, int]]:
    """Only invalidate calls: never undo CombinedKit's quality/no-call decisions."""
    seen: dict[tuple[str, int], str] = {}
    rejected: set[tuple[str, int]] = set()
    with tempfile.TemporaryFile("w+", dir=outdir) as stream:
        run_command(
            ["bcftools", "query", "-f", "%CHROM\t%POS\t[%TGT]\n", vcf],
            stdout=stream,
        )
        stream.seek(0)
        for line in stream:
            chrom, pos, raw = line.rstrip("\n").split("\t")
            key = (_chrom_name(chrom), int(pos))
            genotype = raw.replace("/", "").replace("|", "").upper()
            if len(genotype) == 1:
                genotype *= 2
            if len(genotype) != 2 or not set(genotype) <= set("ACGT"):
                rejected.add(key)
                continue
            genotype = "".join(sorted(genotype))
            if key in seen and seen[key] != genotype:
                rejected.add(key)
            seen[key] = genotype
    return rejected


def _read_calls(combined_kit: str) -> dict[tuple[str, int], str]:
    calls: dict[tuple[str, int], str] = {}
    with open(combined_kit) as source:
        for line in source:
            if line.startswith("#") or not line.strip():
                continue
            _snp_id, chrom, pos, genotype = line.split()[:4]
            key = (_chrom_name(chrom), int(pos))
            # Conflicting records at one position must not silently win.
            if key in calls and calls[key] != genotype:
                calls[key] = "--"
            else:
                calls[key] = genotype
    return calls


def reference_dosage(genotype: str, ref: str, alt: str) -> str:
    """EIGENSTRAT counts reference alleles, not alternate alleles."""
    if len(genotype) == 1:
        genotype *= 2
    if len(genotype) != 2 or not set(genotype) <= {ref, alt}:
        return "9"
    return str(genotype.count(ref))


def write_aadr_outputs(
    panel: str,
    combined_kit: str,
    output_prefix: str,
    *,
    sample_id: str = "sample",
    sex: str = "U",
    lifter: LiftOver | None = None,
    vcf_nocalls: set[tuple[str, int]] | None = None,
) -> dict[str, int]:
    """Write one diploid sample, retaining AADR order, alleles and genetic map."""
    validate_aadr_sample_id(sample_id)
    if sex not in {"U", "M", "F"}:
        raise ValueError("AADR sex must be U, M or F")
    calls = _read_calls(combined_kit)
    counts = {"markers": 0, "called": 0, "incompatible": 0}
    suffixes = (".bed", ".bim", ".fam", ".geno", ".snp", ".ind")
    staged = {suffix: Path(output_prefix + suffix + ".tmp") for suffix in suffixes}
    try:
        with contextlib.ExitStack() as stack:
            bed = stack.enter_context(staged[".bed"].open("wb"))
            bim = stack.enter_context(staged[".bim"].open("w", newline="\n"))
            geno = stack.enter_context(staged[".geno"].open("w", newline="\n"))
            snp = stack.enter_context(staged[".snp"].open("w", newline="\n"))
            bed.write(b"\x6c\x1b\x01")
            for marker in aadr_markers(panel):
                genotype = "--"
                coordinate = _marker_coordinate(marker, lifter)
                if coordinate:
                    chrom, pos, inverted = coordinate
                    genotype = calls.get((chrom, pos), "--")
                    if vcf_nocalls and (chrom, pos) in vcf_nocalls:
                        genotype = "--"
                    if inverted:
                        genotype = genotype.translate(_COMPLEMENT)
                dosage = reference_dosage(genotype, marker.ref, marker.alt)
                counts["markers"] += 1
                counts["called"] += dosage != "9"
                counts["incompatible"] += dosage == "9" and genotype != "--"
                # SNP-major PLINK: first sample in low two bits, padding bits zero.
                bed.write(bytes([_BED_CODE[dosage]]))
                geno.write(dosage + "\n")
                snp.write(
                    f"{marker.snp_id}\t{marker.chrom}\t{marker.genetic_position}\t"
                    f"{marker.position}\t{marker.ref}\t{marker.alt}\n"
                )
                bim.write(
                    f"{marker.chrom}\t{marker.snp_id}\t{Decimal(marker.genetic_position) * 100:f}\t"
                    f"{marker.position}\t{marker.ref}\t{marker.alt}\n"
                )
        sex_code = {"U": 0, "M": 1, "F": 2}[sex]
        staged[".fam"].write_text(f"0 {sample_id} 0 0 {sex_code} -9\n", newline="\n")
        staged[".ind"].write_text(f"{sample_id}\t{sex}\tUnknown\n", newline="\n")
        for suffix, path in staged.items():
            path.replace(output_prefix + suffix)
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)
    logging.info("AADR %s: %s", panel, counts)
    return counts
