import argparse
import csv
import io
import logging
import sys
from pathlib import Path

from wgsextract_cli.commands.info.metrics import sample_sex
from wgsextract_cli.core.alignment_metadata import get_vcf_build
from wgsextract_cli.core.builds import (
    build_from_chr1_length,
    build_from_path,
    is_hg37_build,
    is_hg38_build,
    is_t2t_build,
)
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_assets import input_stem, read_fai_lengths
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    chromosome_aliases,
    ensure_vcf_indexed,
    get_vcf_chr_name,
    verify_paths_exist,
)

VCF_SUFFIXES = (".vcf", ".vcf.gz")


def resolve_lineage_tool(explicit_path: str | None, tool: str, flag: str) -> str:
    """Resolve a lineage tool from its CLI flag/setting or the installed tool."""
    if not explicit_path:
        verify_dependencies([tool])
    path = explicit_path or get_tool_path(tool)
    # A bare command name (no directory part) is resolved like any other tool.
    if path and not Path(path).is_absolute() and Path(path).name == path:
        path = get_tool_path(path) or path
    if not path:
        raise WGSExtractError(f"{tool} path missing.")
    # Only a path the user gave can be missing; a resolved tool may be a
    # `pixi run ...` wrapper command (Haplogrep's jar on Windows), not a file.
    if path == explicit_path:
        verify_paths_exist({flag: path})
    return path


def tool_command(path: str) -> list[str]:
    """Return argv for a tool path; run_command expands `pixi run` wrappers."""
    if path.endswith(".py"):
        return [sys.executable, path]
    return [path]


def extract_vcf_chromosome(input_vcf: str, target_chr: str, out_vcf: str) -> str:
    """Write the records of one chromosome (e.g. MT, Y) of ``input_vcf`` to ``out_vcf``."""
    ensure_vcf_indexed(input_vcf)
    indexed = any(Path(input_vcf + ext).exists() for ext in (".tbi", ".csi"))
    if indexed:
        chrom = get_vcf_chr_name(input_vcf, target_chr)
        region_args = ["-r", chrom]
    else:
        # A plain .vcf can't be indexed; stream it and accept any naming of the contig.
        chrom = target_chr
        region_args = ["-t", ",".join(chromosome_aliases(target_chr))]
    logging.info(f"Filtering VCF to {chrom}...")
    run_command(["bcftools", "view", *region_args, "-Oz", "-o", out_vcf, input_vcf])
    records = run_command(["bcftools", "view", "-H", out_vcf], capture_output=True)
    if not records.stdout.strip():
        raise WGSExtractError(f"{input_vcf} has no variants on chromosome {chrom}.")
    return chrom


def _yleaf_build(build: str | None) -> str:
    if build and is_hg38_build(build):
        return "hg38"
    if build and is_hg37_build(build):
        return "hg19"
    if build and is_t2t_build(build):
        return "t2t"
    logging.warning(f"Build {build} not supported by Yleaf, defaulting to hg38")
    return "hg38"


def cmd_ydna(args: argparse.Namespace) -> None:
    from wgsextract_cli.ydna.yleaf_native.analysis import CREDIT, analyze

    if not args.input:
        raise WGSExtractError(LOG_MESSAGES["input_required"])
    verify_paths_exist({"--input": args.input})

    input_lower = args.input.lower()
    is_vcf = input_lower.endswith((*VCF_SUFFIXES, ".bcf"))
    if not is_vcf and not input_lower.endswith((".bam", ".cram")):
        raise WGSExtractError(
            "Native Yleaf accepts BAM, CRAM or single-sample VCF/BCF. "
            "Align FASTQ with `wgsextract align` first."
        )
    tools = ["samtools", "bcftools"] if is_vcf else ["samtools"]
    verify_dependencies(tools)
    log_dependency_info(tools)

    outdir = str(absolute_path(args.outdir or str(Path(args.input).absolute().parent)))
    yleaf_outdir = str(Path(outdir, "yleaf"))
    logging.debug(f"Input file: {str(absolute_path(args.input))}")
    logging.debug(f"Output directory: {yleaf_outdir}")

    # Yleaf still "predicts" from a female sample's few mismapped chrY reads
    # (BT* with hundreds of exclusions, at full QC score); say there's no Y.
    if (
        input_lower.endswith((".bam", ".cram"))
        and sample_sex(args.input, args.outdir) == "Female"
    ):
        raise WGSExtractError(
            f"{args.input} has almost no chrY reads (the sample appears female), "
            "so there is no Y haplogroup to predict."
        )

    lib = ReferenceLibrary(
        args.ref,
        None if is_vcf else calculate_bam_md5(args.input, None),
        input_path=args.input,
    )
    build = getattr(args, "build", None) or (
        get_vcf_build(args.input) if is_vcf else None
    )
    if not build:
        build = lib.build
    yleaf_build = _yleaf_build(build)
    needs_reference = (
        is_vcf and not getattr(args, "no_ref_inference", False)
    ) or input_lower.endswith(".cram")
    if needs_reference and lib.fasta:
        reference_build = build_from_chr1_length(
            read_fai_lengths(lib.fasta)
        ) or build_from_path(lib.fasta)
        if reference_build and reference_build != yleaf_build:
            raise WGSExtractError(
                f"The selected reference FASTA is {reference_build}, but the "
                f"Yleaf marker build is {yleaf_build}. Pass a matching --ref "
                "FASTA, or use --no-ref-inference for a targeted VCF panel."
            )

    prediction = analyze(
        Path(args.input),
        Path(yleaf_outdir),
        build=yleaf_build,
        tree=getattr(args, "tree", "yfull"),
        reference=Path(lib.fasta) if lib.fasta else None,
        pos_file=Path(args.pos_file) if args.pos_file else None,
        min_reads=getattr(args, "min_reads", 10),
        quality=getattr(args, "base_quality", 20),
        majority=getattr(args, "base_majority", 90),
        min_score=getattr(args, "min_score", 0.95),
        ancient=getattr(args, "ancient", False),
        no_ref_inference=getattr(args, "no_ref_inference", False),
    )

    try:
        with open(prediction, encoding="utf-8") as f:
            content = f.read().strip()
    except OSError as e:
        raise WGSExtractError(f"Yleaf did not write {prediction}: {e}") from e
    rows = list(csv.DictReader(io.StringIO(content), delimiter="\t"))
    # Newer Yleaf writes an NA row instead of a header alone when no call is possible.
    if not rows or not any(
        (row.get("Hg") or "").strip() not in ("", "NA") for row in rows
    ):
        raise WGSExtractError(
            "Yleaf could not predict a Y haplogroup: no Y-chromosome markers "
            "matched or passed QC. Check that the input has chrY reads and that its reference "
            f"build is correct; details are in {yleaf_outdir}."
        )
    from wgsextract_cli.ydna.yleaf_native.data import manifest

    version = manifest()["upstream"]["tag"]
    print(
        f"\n🧬 {CREDIT}\nTables {version}; build {yleaf_build}; "
        f"tree {getattr(args, 'tree', 'yfull')}\n"
        f"Lineage Result ({input_stem(args.input)}):"
    )
    print("-" * 30)
    print(content)
    if getattr(args, "tree_links", False):
        from ._lineage_links import print_tree_links

        for row in rows:
            # The terminal SNP, rather than the long-form lineage, identifies
            # branches in the public trees.
            print_tree_links(row.get("Hg_marker") or row.get("Hg") or "")
    print("-" * 30 + "\n")
