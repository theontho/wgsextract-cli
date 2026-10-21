"""Identify the reference genome an alignment was made against from its header."""

import re
from dataclasses import dataclass

from wgsextract_cli.core.builds import CHR1_LENGTH_BUILDS, build_from_path
from wgsextract_cli.core.constants import REFERENCE_MODELS, REFGEN_BY_SNCOUNT

_VCF_CONTIG_RE = re.compile(r"^##contig=<.*?\bID=([^,>]+).*?\blength=(\d+)")
# The mitochondrial sequence is told apart by its length.
MITO_BY_LENGTH = {16569: "rCRS", 16571: "Yoruba"}


@dataclass(frozen=True)
class ReferenceModel:
    name: str  # build or model, e.g. "hg38", "hs37d5", "T2Tv2.0"
    mito: str  # "rCRS", "Yoruba", or "" when unknown
    fasta: str  # catalog FASTA it matches, or "" when no single one does
    naming: str  # "Chr" (chr1), "Num" (1), or "" when neither
    contigs: int
    evidence: str  # "signature", "chr1 length", "contig count" or "reference"

    def describe(self) -> str:
        """The one-line form `info` prints, e.g. "hg38 (Chr), rCRS, 195 SNs"."""
        naming = f" ({self.naming})" if self.naming else ""
        return f"{self.name}{naming}, {self.mito}, {self.contigs} SNs"


def sequence_lengths(header: str) -> dict[str, int]:
    """Contig lengths from BAM/CRAM @SQ lines, else from VCF ##contig lines."""
    lengths: dict[str, int] = {}
    for line in header.splitlines():
        if not line.startswith("@SQ"):
            continue
        fields = dict(
            part.split(":", 1) for part in line.split("\t")[1:] if ":" in part
        )
        if "SN" in fields and fields.get("LN", "").isdigit():
            lengths[fields["SN"]] = int(fields["LN"])
    if lengths:
        return lengths
    for line in header.splitlines():
        match = _VCF_CONTIG_RE.match(line)
        if match:
            lengths[match.group(1)] = int(match.group(2))
    return lengths


def _naming(lengths: dict[str, int]) -> str:
    if "chr1" in lengths or "CHR1" in lengths:
        return "Chr"
    if "1" in lengths:
        return "Num"
    return ""


def _mito(lengths: dict[str, int]) -> str:
    for name in ("chrM", "MT", "chrMT", "M"):
        if name in lengths:
            return MITO_BY_LENGTH.get(lengths[name], "")
    return ""


def _model_name(fasta: str) -> str | None:
    return next((m[0] for m in REFERENCE_MODELS.values() if m[2] == fasta), None)


def identify_reference(
    header: str, signature: str | None = None, reference_fasta: str | None = None
) -> ReferenceModel | None:
    """The reference a header's sequence dictionary describes, or None if unknown.

    Evidence, strongest first: the exact SN/LN signature of a known reference;
    the chr1 length, which differs between assemblies under any naming; the
    contig count, only when it identifies one reference (a 25-contig dictionary
    can be hg19 or T2T); and last, the name of a FASTA the user supplied.
    """
    lengths = sequence_lengths(header)
    naming, contigs = _naming(lengths), len(lengths)

    if signature and signature in REFERENCE_MODELS:
        name, mito, fasta = REFERENCE_MODELS[signature]
        return ReferenceModel(name, mito, fasta, naming, contigs, "signature")

    count_entry = REFGEN_BY_SNCOUNT.get(contigs)
    count_fasta = str(count_entry[1]) if count_entry else ""
    count_build = build_from_path(count_fasta)

    chr1 = next((lengths[n] for n in ("chr1", "1", "CHR1") if n in lengths), None)
    build = CHR1_LENGTH_BUILDS.get(chr1) if chr1 is not None else None
    if build:
        # Name the matching catalog FASTA only when the count agrees on the build.
        fasta = count_fasta if count_build == build else ""
        name = "T2T" if build == "t2t" else build
        return ReferenceModel(
            name, _mito(lengths), fasta, naming, contigs, "chr1 length"
        )

    # A chr1 of no known human length rules out the human references a contig
    # count could point to, but not a dog's or cat's.
    if count_entry and not (chr1 is not None and count_build):
        name = _model_name(count_fasta) or count_build or count_fasta.split(".")[0]
        return ReferenceModel(
            name, _mito(lengths), count_fasta, naming, contigs, "contig count"
        )

    if reference_fasta:
        build = build_from_path(reference_fasta)
        if build:
            return ReferenceModel(build, "", "", naming, contigs, "reference")
    return None
