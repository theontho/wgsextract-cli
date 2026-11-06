import argparse
import gzip
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from wgsextract_cli.api._cli import cli_handler
from wgsextract_cli.api._params import Params
from wgsextract_cli.api.vcf import (
    VcfAnnotateParams,
    VcfIndelParams,
    VcfSnpParams,
    _VcfFileParams,
)
from wgsextract_cli.core import ultima
from wgsextract_cli.core.alignment_health import warn_about_alignment
from wgsextract_cli.core.builds import is_hg37_build, is_hg38_build, ploidy_for_build
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.genotype_qc import region_targets, warn_low_depth
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.output_checks import vcf_record_count, warn_if_no_variants
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.resource_estimates import (
    estimate_resources,
    preflight_resources,
)
from wgsextract_cli.core.results import current_result
from wgsextract_cli.core.sample_ploidy import sex_samples_args
from wgsextract_cli.core.sequencing import input_platform
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    get_resource_defaults,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    ensure_vcf_indexed,
    ensure_vcf_prepared,
    verify_paths_exist,
)
from wgsextract_cli.core.warnings import print_warning

from .contig_calling import call_small_variants

P = TypeVar("P", bound=Params)


def _select_vcf_input(args: argparse.Namespace) -> str | None:
    """
    Pick the VCF a command should read.

    An explicit --vcf-input wins, then an explicit --input; otherwise the
    configured default VCF, then the (possibly configured) --input.
    """
    input_path = getattr(args, "input", None)
    vcf_input = getattr(args, "vcf_input", None)
    default_vcf = settings.get("default_input_vcf")
    explicit_dests: set[str] = getattr(args, "_explicit_dests", set())

    if vcf_input and ("vcf_input" in explicit_dests or vcf_input != default_vcf):
        return str(vcf_input)
    if "input" in explicit_dests and input_path:
        return str(input_path)
    selected = vcf_input if vcf_input else input_path
    return str(selected) if selected else None


def require_vcf_input(params: _VcfFileParams) -> str:
    """The VCF an operation reads: ``vcf_input``, else ``input``."""
    chosen = params.vcf_input or params.input
    if not chosen:
        raise UsageError(LOG_MESSAGES["input_required"])
    return os.fspath(chosen)


def vcf_input_handler(
    cls: type[P], impl: Callable[..., None], **render: str
) -> Callable[[argparse.Namespace], None]:
    """``cli_handler`` for an operation on a VCF: the CLI's choice between
    --vcf-input (default: config default_input_vcf) and --input
    (``_select_vcf_input``) becomes ``vcf_input``, or stays ``input`` when
    that is the pick (dispatch has recorded it under that role)."""
    handler = cli_handler(cls, impl, **render)

    def cmd(args: argparse.Namespace) -> None:
        explicit: set[str] = getattr(args, "_explicit_dests", set())
        if getattr(args, "vcf_input", None) is None and "vcf_input" not in explicit:
            args.vcf_input = settings.get("default_input_vcf")
        selected = _select_vcf_input(args)
        given = getattr(args, "input", None)
        if selected is not None and given is not None and selected == str(given):
            args.vcf_input = None
        else:
            args.vcf_input = selected
        handler(args)

    cmd.__module__ = impl.__module__
    cmd.__name__ = cmd.__qualname__ = handler.__name__
    cmd.__doc__ = handler.__doc__
    return cmd


@dataclass(frozen=True)
class CallerSetup:
    """What a BAM/CRAM variant caller needs, resolved and checked."""

    threads: str
    outdir: str
    ref: str
    lib: ReferenceLibrary
    platform: str | None


def caller_setup(
    input_file: str | None,
    *,
    threads: int | str | None,
    outdir: str | None,
    ref: str | None,
    region: str | None,
    qc_min_depth: float | None,
) -> CallerSetup:
    """Resolve threads, output folder and reference FASTA for a BAM/CRAM caller,
    and warn about alignment problems and low depth."""
    if not input_file:
        raise UsageError(LOG_MESSAGES["input_required"])

    logging.debug(f"Input file: {str(absolute_path(input_file))}")
    thread_count, _ = get_resource_defaults(threads, None)
    outdir = outdir or str(absolute_path(input_file).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")

    lib = ReferenceLibrary(
        ref, calculate_bam_md5(input_file, None), input_path=input_file
    )
    resolved_ref = lib.fasta
    logging.debug(f"Resolved reference: {resolved_ref}")

    paths_to_check = {"--input": input_file}
    if resolved_ref:
        paths_to_check["--ref"] = resolved_ref
    verify_paths_exist(paths_to_check)
    platform = input_platform(input_file, resolved_ref)
    if platform == "ont":
        logging.info(
            "Detected ONT input. Short-read variant callers are not validated "
            "for ONT; use a platform-specific model/caller where available."
        )

    if not resolved_ref or not Path(resolved_ref).is_file():
        raise WGSExtractError(
            LOG_MESSAGES["ref_required_for"].format(task="variant calling")
        )
    current_result().set_reference(resolved_ref, lib.build)
    warn_about_alignment(input_file, resolved_ref)
    warn_low_depth(
        input_file,
        region_targets(region),
        outdir=outdir,
        ref=resolved_ref,
        min_depth=qc_min_depth,
        region=region,
    )
    return CallerSetup(thread_count, outdir, resolved_ref, lib, platform)


class CallerParams(Protocol):
    """The fields every BAM/CRAM caller's Params has."""

    @property
    def input(self) -> Path: ...
    @property
    def ref(self) -> Path | None: ...
    @property
    def outdir(self) -> Path | None: ...
    @property
    def threads(self) -> int | None: ...
    @property
    def region(self) -> str | None: ...
    @property
    def qc_min_depth(self) -> float | None: ...


def setup_caller(params: CallerParams) -> CallerSetup:
    """``caller_setup`` for a BAM/CRAM caller's Params."""
    return caller_setup(
        os.fspath(params.input),
        threads=params.threads,
        outdir=os.fspath(params.outdir) if params.outdir else None,
        ref=os.fspath(params.ref) if params.ref else None,
        region=params.region,
        qc_min_depth=params.qc_min_depth,
    )


def _ploidy_args(args: argparse.Namespace, lib: ReferenceLibrary) -> list[str]:
    return ploidy_args(
        getattr(args, "ploidy", None), getattr(args, "ploidy_file", None), lib
    )


def ploidy_args(
    ploidy: str | None, ploidy_file: str | None, lib: ReferenceLibrary
) -> list[str]:
    """bcftools call's ploidy options: the explicit choice, else the library's
    ploidy file, else the built-in table for GRCh37/GRCh38."""
    if not ploidy and not ploidy_file:
        ploidy_file = lib.ploidy_file
    if ploidy_file:
        verify_paths_exist({"--ploidy-file": ploidy_file})
        return ["--ploidy-file", ploidy_file]
    if ploidy:
        return ["--ploidy", ploidy]
    # bcftools' predefined GRCh37/GRCh38 ploidy covers both X and chrX naming.
    # T2T's sex-chromosome coordinates differ, so it needs a ploidy file.
    if lib.build and (is_hg37_build(lib.build) or is_hg38_build(lib.build)):
        return ["--ploidy", ploidy_for_build(lib.build)]
    raise WGSExtractError(
        "Ploidy (--ploidy or --ploidy-file) is required and could not be "
        f"auto-resolved from --ref (reference build: {lib.build or 'unknown'})."
    )


@contextmanager
def plain_reference(ref: str, outdir: str) -> Iterator[str]:
    """
    Yield an uncompressed, faidx-indexed FASTA for tools that cannot read .fa.gz.

    Uses the uncompressed sibling (ref.fa next to ref.fa.gz) when present, indexing
    it if needed; otherwise decompresses into a temporary directory under outdir
    that is removed on exit.
    """
    if not ref.lower().endswith(".gz"):
        yield ref
        return
    sibling = ref[:-3]
    if Path(sibling).is_file():
        if not Path(sibling + ".fai").is_file():
            try:
                run_command(["samtools", "faidx", sibling], capture_output=True)
            except (OSError, subprocess.SubprocessError) as e:
                logging.debug(f"Could not index {sibling} in place: {e}")
        if Path(sibling + ".fai").is_file():
            yield sibling
            return
    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_ref_") as tmp:
        target = str(Path(tmp, Path(sibling).name))
        logging.info(f"Decompressing reference for this tool to {target}...")
        try:
            with gzip.open(ref, "rb") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 16 * 1024 * 1024)
            run_command(["samtools", "faidx", target], capture_output=True)
        except (OSError, EOFError, subprocess.SubprocessError) as e:
            raise WGSExtractError(
                f"Failed to prepare uncompressed reference: {e}"
            ) from e
        yield target


def run_small_variants(params: VcfSnpParams | VcfIndelParams) -> None:
    """``vcf snp`` / ``vcf indel``: bcftools mpileup | call into the output folder."""
    indels = isinstance(params, VcfIndelParams)
    task: Literal["snp", "indel"] = "indel" if indels else "snp"
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file = str(params.input)
    setup = setup_caller(params)
    threads, outdir, ref = setup.threads, setup.outdir, setup.ref
    preflight_resources(
        lambda: estimate_resources(
            task,
            input_paths=[input_file],
            output_dir=outdir,
            reference=ref,
            threads=threads,
            region=params.region,
            parallel=params.parallel,
        )
    )
    print_warning("ButtonInDelVCF" if indels else "ButtonSNPVCF", threads=threads)

    out_vcf = str(Path(outdir, "indels.vcf.gz" if indels else "snps.vcf.gz"))
    profile, prior = ultima.bcftools_options(input_file)
    prior = ["-P", "0"] if prior is None else prior
    message = "vcf_calling_indels" if indels else "vcf_calling_snps"
    logging.info(LOG_MESSAGES[message].format(output=out_vcf))
    ploidy = ploidy_args(
        params.ploidy,
        str(params.ploidy_file) if params.ploidy_file else None,
        setup.lib,
    )

    from wgsextract_cli.core.atomic_output import safe_output

    with (
        safe_output(out_vcf) as staged,
        sex_samples_args(input_file, outdir, ploidy, region=params.region) as sex_args,
    ):
        call_small_variants(
            input_file,
            ref,
            staged,
            threads=threads,
            region=params.region,
            profile=profile,
            call_args=[*ploidy, *sex_args, *prior],
            parallel=params.parallel,
            indels=indels,
        )
    ensure_vcf_indexed(out_vcf)
    record_variants(
        out_vcf,
        "indels" if indels else "snps",
        warn_if_no_variants(out_vcf, ref, params.region),
    )


def record_variants(out_vcf: str, role: str, count: int | None) -> None:
    """Register a called VCF and its record count on the result manifest."""
    current_result().add_output(out_vcf, role, metrics={"variants": count})
    current_result().add_metrics(variants=count)


cmd_snp = cli_handler(VcfSnpParams, run_small_variants)
cmd_indel = cli_handler(VcfIndelParams, run_small_variants)


def _annotation_columns(ann_vcf: str) -> str:
    """Infer bcftools annotate -c columns from a tab file header or VCF header."""
    if ann_vcf.lower().endswith(
        (".tab", ".tab.gz", ".txt", ".txt.gz", ".csv", ".csv.gz")
    ):
        col_map = {
            "CHROM": "CHROM",
            "CHR": "CHROM",
            "POS": "POS",
            "ID": "ID",
            "HG": "INFO/HG",
            "RSID": "ID",
        }
        try:
            open_func = gzip.open if ann_vcf.endswith(".gz") else open
            with open_func(ann_vcf, "rt") as f:
                header = f.readline().strip().lstrip("#")
            found_cols = [
                col_map.get(col.upper().lstrip("#"), "-") for col in header.split()
            ]
            if "CHROM" in found_cols and "POS" in found_cols:
                cols = ",".join(found_cols)
                logging.info(f"Auto-resolved columns from header: {cols}")
                return cols
        except (OSError, UnicodeError, ValueError):
            logging.debug("Failed to parse tab header.", exc_info=True)

    if ann_vcf.lower().endswith((".vcf", ".vcf.gz")):
        try:
            res = run_command(["bcftools", "view", "-h", ann_vcf], capture_output=True)
            cols = "ID,INFO/HG" if "ID=HG," in res.stdout else "ID"
            logging.info(f"Auto-resolved VCF columns: {cols}")
            return cols
        except (OSError, subprocess.SubprocessError):
            logging.debug(
                "Failed to inspect VCF header for annotation columns.", exc_info=True
            )

    logging.info("Using default annotation column: ID")
    return "ID"


def run_annotate(params: VcfAnnotateParams) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file = require_vcf_input(params)
    logging.debug(f"Input file: {str(absolute_path(input_file))}")

    outdir = params.outdir or absolute_path(input_file).parent
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")
    out_vcf = str(Path(outdir, "annotated.vcf.gz"))

    ann_vcf = os.fspath(params.ann_vcf) if params.ann_vcf else None
    if not ann_vcf:
        md5_sig = (
            calculate_bam_md5(input_file, None)
            if input_file.lower().endswith((".bam", ".cram"))
            else None
        )
        lib = ReferenceLibrary(params.ref, md5_sig, input_path=input_file)
        if not lib.ref_vcf_tab:
            raise WGSExtractError(
                "--ann-vcf is required and could not be auto-resolved."
            )
        ann_vcf = lib.ref_vcf_tab
        logging.info(f"Auto-resolved annotation file: {ann_vcf}")

    verify_paths_exist({"--input": input_file, "--ann-vcf": ann_vcf})
    cols = params.cols or _annotation_columns(ann_vcf)

    input_vcf = ensure_vcf_prepared(input_file)
    ann_vcf = ensure_vcf_prepared(ann_vcf)

    logging.info(LOG_MESSAGES["vcf_annotating"].format(input=input_vcf, output=out_vcf))
    try:
        run_command(
            [
                "bcftools",
                "annotate",
                "-a",
                ann_vcf,
                "-c",
                cols,
                "-Oz",
                "-o",
                out_vcf,
                input_vcf,
            ]
        )
        ensure_vcf_indexed(out_vcf)
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Annotation failed: {e}") from e
    current_result().add_input(ann_vcf, "annotation")
    record_variants(out_vcf, "annotated", vcf_record_count(out_vcf))


cmd_annotate = vcf_input_handler(VcfAnnotateParams, run_annotate)
