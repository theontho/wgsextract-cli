import argparse
import contextlib
import logging
import subprocess
from pathlib import Path

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api.extract import (
    BamSubsetParams,
    CustomParams,
    MitoFastaParams,
    MitoVcfParams,
    MtBamParams,
    UnmappedParams,
    YdnaBamParams,
    YdnaVcfParams,
    YMtExtractParams,
    _ExtractParams,
)
from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.atomic_output import atomic_outputs_enabled
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.output_checks import (
    bam_has_reads,
    warn_if_no_reads,
    warn_if_no_variants,
)
from wgsextract_cli.core.ref_library.catalog import get_available_genomes
from wgsextract_cli.core.reference_models import identify_reference
from wgsextract_cli.core.results import current_result, warn
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    get_sam_index_cmd,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    ensure_vcf_indexed,
    get_chr_name,
)
from wgsextract_cli.core.warnings import print_warning

from ._extract_helpers import (
    BaseArgs,
    alignments_to_fastq,
    base_args,
    cleanup_files,
    derive_output_path,
    region_or_gene,
)

_SAMTOOLS = ["samtools"]
_CALLING_TOOLS = ["samtools", "bcftools", "tabix"]


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "extract", help="Extract specific chromosomes or unmapped reads."
    )
    ext_subs = parser.add_subparsers(dest="ext_cmd", required=True)
    for name, cls, func in (
        ("mito-fasta", MitoFastaParams, cmd_mito_fasta),
        ("mito-vcf", MitoVcfParams, cmd_mito_vcf),
        ("mt-bam", MtBamParams, cmd_mt_bam),
        ("bam-subset", BamSubsetParams, cmd_bam_subset),
        ("ydna-bam", YdnaBamParams, cmd_ydna_bam),
        ("ydna-vcf", YdnaVcfParams, cmd_ydna_vcf),
        ("y-mt-extract", YMtExtractParams, cmd_y_mt_extract),
        ("unmapped", UnmappedParams, cmd_unmapped),
        ("custom", CustomParams, cmd_custom),
    ):
        sub = ext_subs.add_parser(
            name, parents=[base_parser], help=CLI_HELP[f"cmd_{name}"]
        )
        add_arguments(sub, cls)
        sub.set_defaults(func=func)


def _setup(params: _ExtractParams, tools: list[str]) -> BaseArgs:
    verify_dependencies(tools)
    log_dependency_info(tools)
    base = base_args(
        params.input,
        ref=params.ref,
        outdir=params.outdir,
        threads=params.threads,
        memory=params.memory,
        region=getattr(params, "region", None),
    )
    current_result().set_reference(base.ref)
    return base


def _published(path: str, role: str) -> None:
    current_result().add_output(path, role)


def require_reference(
    resolved_ref: str | None, task: str, input_path: str | None = None
) -> str:
    if not resolved_ref:
        hint = ""
        if input_path:
            header = get_bam_header(input_path) or ""
            signature = calculate_bam_md5(input_path, header=header)
            genomes = get_available_genomes()
            genome = next((g for g in genomes if g["md5"] == signature), None)
            model = identify_reference(header, signature)
            if not genome and model and model.fasta:
                genome = next(
                    (
                        g
                        for g in genomes
                        if g["final"] == model.fasta
                        or g["url"].rsplit("/", 1)[-1] == model.fasta
                    ),
                    None,
                )
            if genome:
                hint = f" Install the matching reference explicitly: wgsextract ref library --install {genome['code']}"
        if not hint:
            hint = " Select the matching reference with wgsextract ref library, then pass its FASTA with --ref."
        raise UsageError(LOG_MESSAGES["ref_required_for"].format(task=task) + hint)
    return resolved_ref


def _extract_regions(
    base: BaseArgs,
    input_path: str,
    out_bam: str,
    regions: list[str],
    bed: str | None = None,
    contents: str | None = None,
    empty_hint: str = "",
) -> None:
    """Extract regions into an indexed BAM; warn if it got no reads, naming what
    it should hold (contents), for outputs the user keeps."""
    from wgsextract_cli.core.atomic_output import (
        atomic_output,
        atomic_outputs_enabled,
        safe_output,
    )

    with cleanup_files(
        on_failure=[out_bam, out_bam + ".bai"],
        preserve_outputs=atomic_outputs_enabled(),
    ):
        with safe_output(out_bam) as staged:
            run_command(
                ["samtools", "view", "-bh", *base.cram_opt, "-@", base.threads]
                + (["-L", bed] if bed else [])
                + ["-o", staged, input_path, *regions]
            )
        command = get_sam_index_cmd(out_bam, threads=base.threads)
        if atomic_outputs_enabled():
            with atomic_output(out_bam + ".bai", retire_indexes=False) as index:
                run_command([*command, index])
        else:
            run_command(command)
    if contents:
        warn_if_no_reads(out_bam, contents, empty_hint)


def _call_haploid_variants(bam: str, ref: str, out_vcf: str) -> int | None:
    """Call variants on a haploid contig (chrM / chrY) into an indexed VCF."""
    from wgsextract_cli.core.atomic_output import atomic_outputs_enabled

    if not atomic_outputs_enabled():
        for stale_index in (out_vcf + ".tbi", out_vcf + ".csi"):
            with contextlib.suppress(FileNotFoundError):
                Path(stale_index).unlink()
    from wgsextract_cli.core.atomic_output import safe_output

    with safe_output(out_vcf) as staged:
        run_pipeline(
            [
                ["bcftools", "mpileup", "-Ou", "-f", ref, bam],
                ["bcftools", "call", "--ploidy", "1", "-mv", "-Oz", "-o", staged],
            ],
            description="Variant calling",
        )
    ensure_vcf_indexed(out_vcf)
    count = warn_if_no_variants(out_vcf, ref)
    current_result().add_metrics(variants=count)
    return count


def _warn_if_female(input_path: str, outdir: str) -> None:
    """A female sample's chrY reads are mismapped (PAR, X-transposed regions):
    the extraction succeeds but holds no Y DNA, so say so."""
    from .info.metrics import sample_sex

    if sample_sex(input_path, outdir) == "Female":
        warn(
            "sample_female",
            f"{input_path} appears female (chrY has under 5% of chrX's reads), so "
            "its chrY reads are mismapped ones: this output holds no Y-DNA.",
        )


def run_mito_fasta(params: MitoFastaParams) -> None:
    base = _setup(params, _CALLING_TOOLS)
    input_path = str(params.input)
    ref = require_reference(base.ref, "mitochondrial extraction", input_path)

    print_warning("ButtonMitoFASTA", threads=base.threads)

    chr_m = get_chr_name(input_path, "MT", base.cram_opt)
    out_bam = derive_output_path(input_path, base.outdir, "_MT_temp.bam")
    out_vcf = derive_output_path(input_path, base.outdir, "_MT_temp.vcf.gz")
    out_fasta = derive_output_path(input_path, base.outdir, "_MT.fasta")

    temp = [out_bam, out_bam + ".bai", out_vcf, out_vcf + ".tbi"]
    try:
        with cleanup_files(
            temp=temp,
            on_failure=[out_fasta],
            preserve_outputs=atomic_outputs_enabled(),
        ):
            _extract_regions(base, input_path, out_bam, [chr_m])
            _call_haploid_variants(out_bam, ref, out_vcf)

            logging.info(LOG_MESSAGES["generating_consensus"].format(output=out_fasta))
            from wgsextract_cli.core.atomic_output import safe_output

            with safe_output(out_fasta) as staged, open(staged, "wb") as f:
                run_pipeline(
                    [
                        ["samtools", "faidx", ref, chr_m],
                        ["bcftools", "consensus", "-f", "-", "-H", "1", out_vcf],
                    ],
                    stdout=f,
                    description="Consensus generation",
                )
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"Mito FASTA extraction failed: {e}") from e
    _published(out_fasta, "mt_fasta")


def run_mito_vcf(params: MitoVcfParams) -> None:
    base = _setup(params, _CALLING_TOOLS)
    input_path = str(params.input)
    ref = require_reference(base.ref, "mitochondrial extraction", input_path)

    print_warning("ButtonMitoVCF", threads=base.threads)

    chr_m = get_chr_name(input_path, "MT", base.cram_opt)
    out_bam = derive_output_path(input_path, base.outdir, "_MT_temp.bam")
    out_vcf = derive_output_path(input_path, base.outdir, "_MT.vcf.gz")

    try:
        with cleanup_files(
            temp=[out_bam, out_bam + ".bai"],
            on_failure=[out_vcf],
            preserve_outputs=atomic_outputs_enabled(),
        ):
            _extract_regions(base, input_path, out_bam, [chr_m])
            logging.info(LOG_MESSAGES["calling_mito_variants"].format(output=out_vcf))
            _call_haploid_variants(out_bam, ref, out_vcf)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"Mito VCF extraction failed: {e}") from e
    _published(out_vcf, "mt_vcf")


def run_mt_bam(params: MtBamParams) -> None:
    base = _setup(params, _SAMTOOLS)
    input_path = str(params.input)

    mt_chr = get_chr_name(input_path, "MT", base.cram_opt)
    out_file = derive_output_path(input_path, base.outdir, "_mtDNA.bam")

    logging.info(LOG_MESSAGES["extracting_mt"].format(mt_chr=mt_chr, output=out_file))
    try:
        _extract_regions(
            base, input_path, out_file, [mt_chr], contents=f"{mt_chr} reads"
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"mtDNA extraction failed: {e}") from e
    _published(out_file, "mt_bam")


def run_bam_subset(params: BamSubsetParams) -> None:
    base = _setup(params, _SAMTOOLS)
    input_path = str(params.input)
    region = region_or_gene(params.region, params.gene, input_path, base.ref)

    out_file = derive_output_path(input_path, base.outdir, "_subset.bam")
    logging.info(
        LOG_MESSAGES["subsetting_file"].format(
            fraction=params.fraction, output=out_file
        )
    )
    try:
        from wgsextract_cli.core.atomic_output import safe_output

        with (
            cleanup_files(
                on_failure=[out_file], preserve_outputs=atomic_outputs_enabled()
            ),
            safe_output(out_file) as staged,
        ):
            run_command(
                ["samtools", "view", "-bh", "-s", str(params.fraction), *base.cram_opt]
                + ["-o", staged, input_path, *([region] if region else [])]
            )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"BAM subsetting failed: {e}") from e
    _published(out_file, "subset_bam")


def run_ydna_bam(params: YdnaBamParams) -> None:
    base = _setup(params, _SAMTOOLS)
    input_path = str(params.input)

    print_warning("ButtonYOnlyBAM", threads=base.threads)
    _warn_if_female(input_path, base.outdir)

    chr_y = get_chr_name(input_path, "Y", base.cram_opt)
    use_poz = params.poz
    out_bam = derive_output_path(
        input_path, base.outdir, "_Y_poz.bam" if use_poz else "_Y.bam"
    )

    logging.info(LOG_MESSAGES["extracting_y"].format(chr_y=chr_y, output=out_bam))
    try:
        if use_poz:
            from wgsextract_cli.core.alignment_metadata import get_bam_header
            from wgsextract_cli.core.reference_models import sequence_lengths

            from ._extract_poz import poz_bed

            contigs = sequence_lengths(get_bam_header(input_path, base.cram_opt))
            with poz_bed(contigs, chr_y, base.outdir) as bed:
                _extract_regions(
                    base,
                    input_path,
                    out_bam,
                    [chr_y],
                    bed=bed,
                    contents=f"{chr_y} reads overlapping Poz regions",
                    empty_hint="the Poz preset covers only selected Y regions",
                )
        else:
            _extract_regions(
                base,
                input_path,
                out_bam,
                [chr_y],
                contents=f"{chr_y} reads",
                empty_hint="a female sample has none",
            )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Y BAM extraction failed: {e}") from e
    _published(out_bam, "y_bam")


def run_ydna_vcf(params: YdnaVcfParams) -> None:
    use_ybrowse = params.ybrowse
    base = _setup(params, [*_CALLING_TOOLS, "bgzip"] if use_ybrowse else _CALLING_TOOLS)
    input_path = str(params.input)
    if use_ybrowse:
        from ._extract_ybrowse import call_ybrowse

        call_ybrowse(input_path, base)
        return
    ref = require_reference(base.ref, "Y extraction", input_path)

    print_warning("ButtonYOnlyVCF", threads=base.threads)
    _warn_if_female(input_path, base.outdir)

    chr_y = get_chr_name(input_path, "Y", base.cram_opt)
    out_bam = derive_output_path(input_path, base.outdir, "_Y_temp.bam")
    out_vcf = derive_output_path(input_path, base.outdir, "_Y.vcf.gz")

    try:
        with cleanup_files(
            temp=[out_bam, out_bam + ".bai"],
            on_failure=[out_vcf],
            preserve_outputs=atomic_outputs_enabled(),
        ):
            _extract_regions(base, input_path, out_bam, [chr_y])
            logging.info(LOG_MESSAGES["calling_y_variants"].format(output=out_vcf))
            _call_haploid_variants(out_bam, ref, out_vcf)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"Y VCF extraction failed: {e}") from e
    _published(out_vcf, "y_vcf")


def run_y_mt_extract(params: YMtExtractParams) -> None:
    base = _setup(params, _SAMTOOLS)
    input_path = str(params.input)

    print_warning("ButtonYMTBAM", threads=base.threads)
    _warn_if_female(input_path, base.outdir)

    chr_y = get_chr_name(input_path, "Y", base.cram_opt)
    chr_m = get_chr_name(input_path, "MT", base.cram_opt)
    out_bam = derive_output_path(input_path, base.outdir, "_Y_MT.bam")

    logging.info(LOG_MESSAGES["extracting_y_mt"].format(output=out_bam))
    try:
        _extract_regions(
            base,
            input_path,
            out_bam,
            [chr_y, chr_m],
            contents=f"{chr_y} or {chr_m} reads",
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Y+MT extraction failed: {e}") from e
    _published(out_bam, "y_mt_bam")


def run_unmapped(params: UnmappedParams) -> None:
    base = _setup(params, _SAMTOOLS)
    input_path = str(params.input)

    print_warning("ButtonUnmappedBAM", threads=base.threads)

    out_bam = derive_output_path(input_path, base.outdir, "_unmapped.bam")

    logging.info(LOG_MESSAGES["extracting_unmapped"].format(output=out_bam))
    try:
        with cleanup_files(on_failure=[out_bam]):
            # -f 4 gets unmapped reads
            run_command(
                [
                    "samtools",
                    "view",
                    "-bh",
                    "-f",
                    "4",
                    *base.cram_opt,
                    "-@",
                    base.threads,
                ]
                + ["-o", out_bam, input_path]
            )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Unmapped extraction failed: {e}") from e
    _published(out_bam, "unmapped_bam")
    if bam_has_reads(out_bam) is False:
        # Unlike the other extractions, none can be right: an aligner that
        # dropped its unmapped reads.
        logging.info(f"{input_path} has no unmapped reads.")
    if params.fastq:
        fastqs = {
            role: derive_output_path(input_path, base.outdir, f"_unmapped_{tag}")
            for role, tag in (
                ("fastq_r1", "R1.fastq.gz"),
                ("fastq_r2", "R2.fastq.gz"),
                ("fastq_other", "SE.fastq.gz"),
                ("fastq_singletons", "singletons.fastq.gz"),
            )
        }
        alignments_to_fastq(
            out_bam,
            fastqs["fastq_r1"],
            fastqs["fastq_r2"],
            threads=base.threads,
            memory=base.memory,
            temp_parent=base.outdir,
            out_other=fastqs["fastq_other"],
            out_singletons=fastqs["fastq_singletons"],
        )
        for role, path in fastqs.items():
            if Path(path).is_file():
                _published(path, role)


def run_custom(params: CustomParams) -> None:
    base = _setup(params, _SAMTOOLS)
    input_path = str(params.input)
    region = region_or_gene(params.region, params.gene, input_path, base.ref)
    if not region:
        raise UsageError("--region or --gene is required for custom extraction.")

    is_bed = Path(region).is_file()
    label = Path(region).name if is_bed else region
    region_suffix = label.replace(":", "_").replace(",", "_")
    out_bam = derive_output_path(input_path, base.outdir, f"_{region_suffix}.bam")

    logging.info(
        LOG_MESSAGES["extracting_region"].format(region=region, output=out_bam)
    )
    try:
        if is_bed:
            _extract_regions(
                base, input_path, out_bam, [], bed=region, contents=f"reads in {label}"
            )
        else:
            # Split comma-separated regions (e.g. chr1:1-100,chr2:200-300)
            _extract_regions(
                base,
                input_path,
                out_bam,
                region.split(","),
                contents=f"reads in {label}",
            )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Custom extraction failed: {e}") from e
    _published(out_bam, "region_bam")


cmd_mito_fasta = cli_handler(MitoFastaParams, run_mito_fasta)
cmd_mito_vcf = cli_handler(MitoVcfParams, run_mito_vcf)
cmd_mt_bam = cli_handler(MtBamParams, run_mt_bam)
cmd_bam_subset = cli_handler(BamSubsetParams, run_bam_subset)
cmd_ydna_bam = cli_handler(YdnaBamParams, run_ydna_bam)
cmd_ydna_vcf = cli_handler(YdnaVcfParams, run_ydna_vcf)
cmd_y_mt_extract = cli_handler(YMtExtractParams, run_y_mt_extract)
cmd_unmapped = cli_handler(UnmappedParams, run_unmapped)
cmd_custom = cli_handler(CustomParams, run_custom)
