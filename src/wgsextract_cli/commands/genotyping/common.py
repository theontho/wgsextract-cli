"""Input handling shared by the `typing` commands."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from wgsextract_cli.core.builds import build_from_chr1_length
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.paths import absolute_path, expand_path
from wgsextract_cli.core.pixi_tools import pixi_env_tool
from wgsextract_cli.core.reference_assets import input_stem
from wgsextract_cli.core.reference_resolver import header_contig_lengths
from wgsextract_cli.core.results import current_result
from wgsextract_cli.core.samtools_commands import alignment_index
from wgsextract_cli.core.tool_commands import _tool_command_parts
from wgsextract_cli.core.utils import (
    MissingDependencyError,
    WGSExtractError,
    get_resource_defaults,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    get_bam_header,
    resolve_reference,
    verify_paths_exist,
)

if TYPE_CHECKING:
    from wgsextract_cli.api.genotyping import _TypingParams

_ALT_CONTIG = re.compile(r"^(?:chr)?(?P<chrom>[0-9XY]+)_\w+_alt$")


def require_index(path: str) -> None:
    """Typing reads regions of the input, which needs its index."""
    if not alignment_index(path):
        raise WGSExtractError(
            f"{path} needs an index (.bai/.crai): wgsextract bam index -i {path}"
        )


@dataclass(frozen=True)
class Alignment:
    path: str
    lengths: dict[str, int]
    build: str  # "hg19" or "hg38"
    chr_prefix: bool
    reference: str | None
    stem: str
    outdir: str
    threads: str

    @property
    def is_cram(self) -> bool:
        return self.path.lower().endswith(".cram")

    @property
    def ref_opt(self) -> list[str]:
        return ["-T", self.reference] if self.reference else []

    def require_reference(self) -> str:
        if not self.reference:
            raise WGSExtractError(
                "--ref is required: the reference FASTA the input was aligned to."
            )
        return self.reference

    def contig(self, chrom: str) -> str:
        return f"chr{chrom}" if self.chr_prefix else chrom

    def alt_contigs(self, chrom: str) -> list[str]:
        """GRCh38 ALT contigs of a chromosome (e.g. chr22_KI270879v1_alt)."""
        return [
            name
            for name in self.lengths
            if (match := _ALT_CONTIG.match(name)) and match["chrom"] == chrom
        ]


def open_alignment(params: _TypingParams) -> Alignment:
    """The BAM/CRAM in ``input`` with its build, contig names and reference."""
    path = expand_path(params.input)
    verify_paths_exist({"--input": path})
    if not path.lower().endswith((".bam", ".cram")):
        raise WGSExtractError("--input must be a BAM or CRAM file.")
    verify_dependencies(["samtools"])
    require_index(path)

    header = get_bam_header(path)
    if not header:
        raise WGSExtractError(f"Could not read the header of {path}.")
    lengths = header_contig_lengths(header)
    build = build_from_chr1_length(lengths)
    if build not in ("hg19", "hg38"):
        raise WGSExtractError(
            f"{path} isn't aligned to GRCh37/hg19 or GRCh38 (detected: "
            f"{build or 'unknown'}); typing needs one of those."
        )

    ref = expand_path(params.ref) if params.ref else None
    if ref and not Path(ref).exists():
        raise WGSExtractError(f"Reference not found: {ref}")
    reference = resolve_reference(ref, calculate_bam_md5(path, header=header), path)
    if not (reference and Path(reference).is_file()):
        reference = None

    threads, _memory = get_resource_defaults(params.threads, None)
    outdir = expand_path(params.outdir or absolute_path(path).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    aln = Alignment(
        path=path,
        lengths=lengths,
        build=build,
        chr_prefix="chr1" in lengths,
        reference=reference,
        stem=input_stem(path),
        outdir=outdir,
        threads=threads,
    )
    if aln.is_cram:
        aln.require_reference()
    return aln


def env_tool(tool: str, env: str, note: str = "") -> list[str]:
    """Command running `tool` from its optional Pixi environment."""
    command = pixi_env_tool(tool)
    if not command:
        raise MissingDependencyError(
            f"{tool} runs from the optional `{env}` Pixi environment (Linux and "
            f"macOS), which isn't available: pixi install -e {env}{note}"
        )
    return _tool_command_parts(command)


def write_bed(path: str, regions: Iterable[tuple[str, int, int]]) -> str:
    with open(path, "w", newline="\n") as f:
        f.writelines(f"{chrom}\t{start}\t{end}\n" for chrom, start, end in regions)
    return path


def write_outside_calls(path: str, calls: list[tuple[str, str]]) -> str | None:
    """A PharmCAT outside-call file (gene, diplotype), or None without calls."""
    if not calls:
        if Path(path).exists():
            Path(path).unlink()
        return None
    # PharmCAT reads outside calls as UTF-8 (CYP2D6 copy numbers use "≥").
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(f"{gene}\t{diplotype}\n" for gene, diplotype in calls)
    current_result().add_output(path, "pharmcat_outside_call")
    logging.info(
        f"PharmCAT outside call: {path} (wgsextract vcf pharmcat --outside-call)"
    )
    return path
