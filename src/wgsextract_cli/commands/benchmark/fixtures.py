import argparse
from pathlib import Path

from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import chromosome_aliases

from .execution import (
    _run_cli_step_with_io,
)
from .models import (
    BenchmarkResult,
    _default_heavy_region,
    _read_fai,
)


def _run_cli_pipe_step(
    args: argparse.Namespace,
    name: str,
    slug: str,
    command_args: list[str],
    input_file: Path,
    output_file: Path,
    output_dir: Path,
    logs_dir: Path,
    expected_outputs: list[Path],
    command_label: str | None = None,
) -> BenchmarkResult:
    return _run_cli_step_with_io(
        args,
        name,
        slug,
        command_args,
        output_dir,
        logs_dir,
        expected_outputs,
        command_label,
        stdin_file=input_file,
        stdout_file=output_file,
        stdout_log_text=str(output_file) + "\n",
    )


def _prepare_trio_vcf_inputs(source_vcf: Path, outputs: dict[str, Path]) -> None:
    for sample_name, output_vcf in outputs.items():
        sample_file = output_vcf.with_suffix(".sample.txt")
        sample_file.write_text(f"{sample_name}\n", encoding="utf-8")
        run_command(
            [
                "bcftools",
                "reheader",
                "-s",
                str(sample_file),
                "-o",
                str(output_vcf),
                str(source_vcf),
            ]
        )
        run_command(["tabix", "-f", "-p", "vcf", str(output_vcf)])


def _prepare_analyze_batch_fixture(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "benchmark_sample.bam").write_bytes(b"benchmark bam placeholder\n")
    (output_dir / "benchmark_sample.vcf.gz").write_bytes(b"benchmark vcf placeholder\n")
    (output_dir / "benchmark_sample.vcf.gz.tbi").write_bytes(b"benchmark index\n")


def _prepare_repair_fixtures(sam_path: Path, vcf_path: Path) -> None:
    sam_path.parent.mkdir(parents=True, exist_ok=True)
    sam_path.write_text(
        "@HD\tVN:1.6\tSO:coordinate\n"
        "@SQ\tSN:chr1\tLN:1000\n"
        "read name with spaces\t0\tchr1\t1\t60\t10M\t*\t0\t0\tACGTACGTAC\tIIIIIIIIII\n",
        encoding="utf-8",
    )
    vcf_path.write_text(
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr1\t1\t.\tA\tC\t50\tPASS!BAD\t.\n",
        encoding="utf-8",
    )


def _trio_benchmark_region(ref_path: Path) -> str | None:
    fai_path = Path(str(ref_path) + ".fai")
    if not fai_path.exists():
        return None
    contigs = _read_fai(fai_path)
    for chrom, length in contigs:
        if chrom.upper().replace("CHR", "") in {"M", "MT"}:
            return f"{chrom}:1-{length}"
    return _default_heavy_region(ref_path)


def _target_ranges(
    contigs: list[tuple[str, int]], region: str | None
) -> list[tuple[str, int, int]]:
    if not region:
        return [(chrom, 1, length) for chrom, length in contigs]

    chrom_part, has_range, range_part = region.partition(":")
    aliases = set(chromosome_aliases(chrom_part))
    matching = [(chrom, length) for chrom, length in contigs if chrom in aliases]

    ranges = []
    for chrom, length in matching:
        start = 1
        end = length
        if has_range:
            raw_start, _sep, raw_end = range_part.replace(",", "").partition("-")
            start = max(1, int(raw_start))
            end = min(length, int(raw_end) if raw_end else length)
        if start <= end:
            ranges.append((chrom, start, end))
    return ranges


def _create_target_snp_tab(
    ref_path: Path, target_tab_gz: Path, target_count: int, region: str | None
) -> None:
    fai_path = Path(str(ref_path) + ".fai")
    if not fai_path.exists():
        run_command(["samtools", "faidx", str(ref_path)])

    contigs = _read_fai(fai_path)
    ranges = _target_ranges(contigs, region)
    if not ranges:
        raise WGSExtractError(f"No reference contigs match benchmark region: {region}")

    total_bases = sum(end - start + 1 for _name, start, end in ranges)
    tab_path = target_tab_gz.with_suffix("")
    bases = ["A", "C", "G", "T"]

    with open(tab_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("#CHROM\tPOS\tID\tREF\tALT\n")
        snp_index = 1
        for chrom, start, end in ranges:
            length = end - start + 1
            contig_targets = max(1, round(target_count * (length / total_bases)))
            step = max(1, length // (contig_targets + 1))
            for offset in range(step, length, step):
                if snp_index > target_count and len(ranges) == 1:
                    break
                pos = start + offset
                ref = bases[(snp_index + len(chrom)) % len(bases)]
                alt = bases[(snp_index + len(chrom) + 1) % len(bases)]
                if ref == alt:
                    alt = bases[(bases.index(ref) + 1) % len(bases)]
                handle.write(f"{chrom}\t{pos}\tbench_rs{snp_index}\t{ref}\t{alt}\n")
                snp_index += 1

    run_command(["bgzip", "-f", str(tab_path)])
    run_command(["tabix", "-f", "-p", "vcf", str(target_tab_gz)])
