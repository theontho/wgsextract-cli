"""Pet Analysis command for aligning and calling variants in non-human species."""

import argparse
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api.pet import PetAlignParams
from wgsextract_cli.core.alignment_memory import check_mapping_memory
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.genome_library import pair_stem
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.output_checks import warn_if_no_variants
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.read_counts import fastq_input_reads
from wgsextract_cli.core.reference_assets import is_fasta_path
from wgsextract_cli.core.results import current_result
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    get_resource_defaults,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import verify_paths_exist
from wgsextract_cli.core.warnings import warn_alignment_sort_space

from ._extract_helpers import cleanup_files
from .align import (
    BWA_BATCH_BASES,
    AlignJob,
    add_read_inputs,
    duplicate_marker,
    ensure_bwa_index,
    read_group,
    reads_stem,
    record_alignment,
    run_streamed_alignment,
    short_read_aligner,
    short_read_sort_memory,
)

_SPECIES_REFERENCES = {
    "dog": "GCF_011100685.1_UU_Cfam_GSD_1.0_genomic.fna.gz",
    "cat": "GCF_018350175.1_F.catus_Fca126_mat1.0_genomic.fna.gz",
}


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "pet-align", parents=[base_parser], help=CLI_HELP["cmd_pet-align"]
    )
    add_arguments(parser, PetAlignParams)
    parser.set_defaults(func=cli_handler(PetAlignParams, run_pet))


@dataclass(kw_only=True)
class PetJob(AlignJob):
    """A pet alignment: an alignment of one species' reads, then calling."""

    r1: str = field()  # resets AlignJob.r1's None default
    species: str


def run_pet(params: PetAlignParams) -> None:
    """The ``pet-align`` operation (``api.pet.align``)."""
    add_read_inputs(params.r1, params.r2)
    run(PetJob.of(params))


def run(job: PetJob) -> None:
    verify_dependencies(["bwa", "samtools", "bcftools"])
    log_dependency_info(["bwa", "samtools", "bcftools"])

    paths_to_check = {"--r1": job.r1}
    if job.r2:
        paths_to_check["--r2"] = job.r2
    verify_paths_exist(paths_to_check)

    threads, memory = get_resource_defaults(job.threads, job.memory)

    if not job.ref:
        raise UsageError(LOG_MESSAGES["ref_required"])
    # A FASTA path is used as given; anything else is a reference library.
    if is_fasta_path(job.ref):
        ref_file = job.ref
        hint = ""
    else:
        ref_file = str(Path(job.ref, "genomes", _SPECIES_REFERENCES[job.species]))
        hint = " Download it with `wgsextract ref library --install`."
    logging.debug(f"Resolved reference: {ref_file}")
    if not Path(ref_file).is_file():
        raise WGSExtractError(
            f"Reference genome for {job.species} not found at {ref_file}.{hint}"
        )
    # bwa-mem2 when the reference has its index (`ref index --aligner bwa-mem2`).
    samblaster = duplicate_marker(paired=bool(job.r2)) == "samblaster"
    reads = fastq_input_reads(job.r1, job.r2)
    aligner = short_read_aligner(
        "auto",
        ref_file,
        threads=threads,
        memory=memory,
        samblaster=samblaster,
        reads=reads,
    )
    memory = short_read_sort_memory(aligner, memory)
    check_mapping_memory(
        ref_file,
        aligner,
        threads=threads,
        memory=memory,
        samblaster=samblaster,
        reads=reads,
    )
    if aligner == "bwa":
        ensure_bwa_index(ref_file)

    outdir = job.outdir or str(absolute_path(job.r1).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    warn_alignment_sort_space(outdir, reads)
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")
    stem = reads_stem(job.r1)
    if job.r2:
        # Both mates go into one output, so it shouldn't be named after R1.
        stem = pair_stem(stem)
    base_name = f"{stem}_{job.species}"
    ext = ".cram" if job.format == "CRAM" else ".bam"
    out_bam = str(Path(outdir, base_name + ext))
    out_vcf = str(Path(outdir, f"{base_name}.vcf.gz"))

    logging.info(LOG_MESSAGES["pet_aligning"].format(species=job.species))
    align_cmd = [
        aligner, "mem", "-K", BWA_BATCH_BASES, "-t", threads,
        "-R", read_group(stem),
    ]  # fmt: skip
    align_cmd += [ref_file, job.r1]
    try:
        run_streamed_alignment(
            align_cmd + ([job.r2] if job.r2 else []),
            out_bam,
            threads=threads,
            memory=memory,
            fmt=job.format,
            ref=ref_file,
            duplicates=duplicate_marker(paired=bool(job.r2)),
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Alignment error: {e}") from e
    record_alignment(out_bam, aligner, ref_file)

    logging.info(LOG_MESSAGES["pet_calling"])
    try:
        with cleanup_files(on_failure=[out_vcf, out_vcf + ".csi"]):
            run_pipeline(
                [
                    ["bcftools", "mpileup", "--threads", threads, "-Ou", "-f", ref_file]
                    + [out_bam],
                    ["bcftools", "call", "--threads", threads, "-mv", "-Oz"]
                    + ["-o", out_vcf],
                ],
                description="Variant calling",
            )
            logging.info("Indexing VCF...")
            run_command(["bcftools", "index", "-f", out_vcf])
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Variant calling error: {e}") from e
    count = warn_if_no_variants(out_vcf, ref_file)
    current_result().add_output(out_vcf, "variants", metrics={"variants": count})
    current_result().add_metrics(species=job.species, variants=count)

    logging.info(LOG_MESSAGES["pet_complete"])
    logging.info(LOG_MESSAGES["pet_results"].format(bam=out_bam, vcf=out_vcf))
