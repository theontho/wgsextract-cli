"""Read-backed phasing of a sample's calls with its long reads (optional).

``vcf phase`` phases the heterozygous variants of one sample's VCF with the
sample's BAM/CRAM, using WhatsHap or LongPhase from the opt-in ``phasing``
Pixi environment (checked, never installed), and optionally tags the reads
with their haplotype (HP) and phase block (PS).

WhatsHap is single-threaded, so the contigs are split into ``--threads``
batches of about equal size, phased in parallel and merged. LongPhase is
multi-threaded and needs the reads' platform (ONT or PacBio).
"""

import heapq
import logging
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from wgsextract_cli.api._cli import cli_handler
from wgsextract_cli.api.vcf import VcfPhaseParams
from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.atomic_output import safe_output
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.results import Scalar, current_result, warn
from wgsextract_cli.core.samtools_commands import alignment_index
from wgsextract_cli.core.ultima import read_groups
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import ensure_vcf_indexed, verify_paths_exist

from .basic import setup_caller

LONG_READ_PLATFORMS = {"ont": "--ont", "pacbio": "--pb"}
WINDOWS_UNSUPPORTED = (
    "WhatsHap and LongPhase are not available on Windows (Bioconda builds them "
    "for Linux and macOS only). Run `vcf phase` on Linux or macOS, for example "
    "under WSL2."
)


def require_supported_platform() -> None:
    if sys.platform == "win32":
        raise WGSExtractError(WINDOWS_UNSUPPORTED)


def require_a_phaser(choice: str | None) -> None:
    """Fail early (before the reads are inspected) when no phaser is installed."""
    if choice:
        verify_dependencies([choice])
    elif not get_tool_path("longphase"):
        verify_dependencies(["whatshap"])


def choose_phaser(choice: str | None, platform: str | None) -> str:
    """``choice``, else LongPhase for ONT/PacBio reads when it is installed,
    else WhatsHap."""
    if choice == "longphase" and platform not in LONG_READ_PLATFORMS:
        raise UsageError(
            "LongPhase phases Nanopore or PacBio reads only, and these reads' "
            f"platform is {platform or 'unknown'}. Use --phaser whatshap."
        )
    if choice:
        verify_dependencies([choice])
        return choice
    if platform in LONG_READ_PLATFORMS and get_tool_path("longphase"):
        return "longphase"
    verify_dependencies(["whatshap"])
    return "whatshap"


def vcf_samples(vcf: str) -> list[str]:
    out = run_command(["bcftools", "query", "-l", vcf], capture_output=True).stdout
    return [line for line in out.splitlines() if line]


def phase_sample(vcf: str, header: str) -> str:
    """The VCF sample to phase: its only one, else the reads' @RG SM."""
    samples = vcf_samples(vcf)
    if not samples:
        raise UsageError(f"{vcf} has no samples (genotypes) to phase.")
    names = {group["SM"] for group in read_groups(header) if group.get("SM")}
    # The phasers use every read (their @RG SM may not match the VCF's name),
    # so another individual's reads would be phased in.
    if len(names) > 1:
        raise UsageError(
            f"The reads hold several samples ({', '.join(sorted(names))}); "
            "phasing needs one sample's reads. Extract that sample's read "
            "groups first, e.g. `samtools view -r GROUP`."
        )
    if len(samples) == 1:
        return samples[0]
    matches = [s for s in samples if s in names]
    if len(matches) == 1:
        return matches[0]
    raise UsageError(
        f"{vcf} has samples {', '.join(samples)}; the reads' @RG SM "
        f"({', '.join(sorted(names)) or 'none'}) must name exactly one of them."
    )


def _tool_env() -> dict[str, str]:
    # WhatsHap runs its own Python; don't leak ours into it.
    env = dict(os.environ)
    for var in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(var, None)
    return env


def _has_index(vcf: str) -> bool:
    return any(Path(vcf + ext).is_file() for ext in (".tbi", ".csi"))


def prepare_vcf(vcf: str, sample: str, region: str | None, out: str) -> None:
    """The sample's records (in ``region``), without any previous phasing,
    bgzipped and indexed."""
    select = []
    if region:
        select = ["-r" if _has_index(vcf) else "-t", region]
    # Old phase sets go: WhatsHap refuses GIAB's string PS, and either tool
    # would leave stale ones on the variants it doesn't phase.
    run_pipeline(
        [
            ["bcftools", "view", "-s", sample, *select, "-Ou", vcf],
            ["bcftools", "annotate", "-x", "FORMAT/PS", "-Oz", "-o", out],
        ],
        description="Preparing the VCF to phase",
    )
    ensure_vcf_indexed(out)
    records = run_command(["bcftools", "index", "-n", out], capture_output=True)
    if records.stdout.strip() == "0":
        where = f" in {region}" if region else ""
        raise UsageError(f"{vcf} has no variants for {sample}{where} to phase.")


def contig_batches(vcf: str, batches: int) -> list[list[str]]:
    """The VCF's contigs in at most ``batches`` groups of about equal record
    counts (largest first, each to the smallest group)."""
    out = run_command(["bcftools", "index", "-s", vcf], capture_output=True).stdout
    counts = []
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) >= 3 and fields[2].isdigit() and int(fields[2]):
            counts.append((int(fields[2]), fields[0]))
    groups: list[tuple[int, int, list[str]]] = [
        (0, i, []) for i in range(min(batches, len(counts)))
    ]
    for count, contig in sorted(counts, reverse=True):
        total, i, members = heapq.heappop(groups)
        members.append(contig)
        heapq.heappush(groups, (total + count, i, members))
    return [members for _total, _i, members in sorted(groups, key=lambda g: g[1])]


def whatshap_phase_command(
    vcf: str, reads: str, ref: str, sample: str, output: str
) -> list[str]:
    # Reads' @RG SM may differ from the VCF's sample name (one sample is phased).
    return [
        "whatshap", "phase", "--reference", ref, "--ignore-read-groups",
        "--sample", sample, "-o", output, vcf, reads,
    ]  # fmt: skip


def run_whatshap(
    vcf: str, reads: str, ref: str, sample: str, threads: int, work: str, out: str
) -> None:
    batches = contig_batches(vcf, threads)
    if not batches:
        run_command(["bcftools", "view", "-Oz", "-o", out, vcf])
        return
    logging.info(
        f"Phasing {sum(map(len, batches))} contigs with WhatsHap in "
        f"{len(batches)} parallel batch(es)."
    )
    parts = []
    for i, contigs in enumerate(batches):
        part = str(Path(work, f"batch{i}.vcf.gz"))
        # A regions file, as `-r` can't parse contig names with ':' (HLA-*).
        regions = str(Path(work, f"batch{i}.regions.txt"))
        with open(regions, "w", newline="\n") as f:
            f.writelines(f"{contig}\t1\t2147483647\n" for contig in contigs)
        run_command(["bcftools", "view", "-R", regions, "-Oz", "-o", part, vcf])
        parts.append((part, str(Path(work, f"batch{i}.phased.vcf.gz"))))
    env = _tool_env()
    cancelled = Event()

    def phase(part: tuple[str, str]) -> None:
        if cancelled.is_set():
            return
        run_pipeline(
            [whatshap_phase_command(part[0], reads, ref, sample, part[1])],
            stdin=subprocess.DEVNULL,
            env=env,
            description="WhatsHap phase",
            cancel_event=cancelled,
        )

    # A failed or interrupted batch stops the others rather than waiting on them.
    with ThreadPoolExecutor(max_workers=len(parts)) as pool:
        futures = [pool.submit(phase, part) for part in parts]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            cancelled.set()
            for future in futures:
                future.cancel()
            raise
    concat = str(Path(work, "concat.bcf"))
    run_command(["bcftools", "concat", "-Ob", "-o", concat, *(p for _in, p in parts)])
    run_command(["bcftools", "sort", "-T", work, "-Oz", "-o", out, concat])


def longphase_phase_command(
    vcf: str,
    reads: str,
    ref: str,
    sample: str,
    platform: str,
    threads: int,
    prefix: str,
) -> list[str]:
    return [
        "longphase", "phase", "-s", vcf, "-b", reads, "-r", ref,
        LONG_READ_PLATFORMS[platform], "--indels", "--sample", sample,
        "-t", str(threads), "-o", prefix,
    ]  # fmt: skip


def run_longphase(
    vcf: str, reads: str, ref: str, sample: str, platform: str, threads: int,
    work: str, out: str,
) -> None:  # fmt: skip
    prefix = str(Path(work, "longphase"))
    run_command(
        longphase_phase_command(vcf, reads, ref, sample, platform, threads, prefix),
        stdin=subprocess.DEVNULL,
    )
    produced = prefix + ".vcf"
    if not Path(produced).is_file():
        raise WGSExtractError("LongPhase failed to produce a phased VCF.")
    run_command(["bcftools", "view", "-Oz", "-o", out, produced])


@dataclass(frozen=True)
class PhaseStats:
    heterozygous: int
    phased: int
    blocks: list[tuple[str, str, int, int, int]]  # contig, PS, first, last, sites

    @property
    def phased_pct(self) -> float:
        return (
            round(100 * self.phased / self.heterozygous, 2)
            if self.heterozygous
            else 0.0
        )

    @property
    def block_n50(self) -> int:
        """N50 of the blocks' spans: last minus first variant, as WhatsHap measures."""
        spans = sorted(
            (last - first for _c, _p, first, last, _n in self.blocks), reverse=True
        )
        half, total = sum(spans) / 2, 0
        for span in spans:
            total += span
            if total >= half:
                return span
        return 0


def phase_stats(vcf: str) -> PhaseStats:
    """Heterozygous calls, those phased, and the phase blocks (by PS)."""
    out = run_command(
        [
            "bcftools",
            "query",
            "-i",
            'GT="het"',
            "-f",
            "%CHROM\t%POS[\t%GT\t%PS]\n",
            vcf,
        ],
        capture_output=True,
    ).stdout
    heterozygous = phased = 0
    blocks: dict[tuple[str, str], list[int]] = defaultdict(list)
    for line in out.splitlines():
        contig, pos, gt, ps = line.split("\t")[:4]
        heterozygous += 1
        if "|" in gt and ps not in (".", ""):
            phased += 1
            blocks[(contig, ps)].append(int(pos))
    rows = [
        (contig, ps, min(sites), max(sites), len(sites))
        for (contig, ps), sites in blocks.items()
    ]
    return PhaseStats(heterozygous, phased, sorted(rows, key=lambda r: (r[0], r[2])))


def write_blocks(stats: PhaseStats, path: str) -> None:
    with safe_output(path) as staged, open(staged, "w", newline="\n") as f:
        f.write("contig\tphase_set\tfirst\tlast\tspan_bp\tvariants\n")
        for contig, ps, first, last, sites in stats.blocks:
            f.write(f"{contig}\t{ps}\t{first}\t{last}\t{last - first}\t{sites}\n")


def haplotag_command(
    phaser: str, vcf: str, reads: str, ref: str, sample: str, threads: int,
    output: str, regions: Sequence[str],
) -> list[str]:  # fmt: skip
    if phaser == "whatshap":
        command = [
            "whatshap", "haplotag", "--reference", ref, "--ignore-read-groups",
            "--sample", sample, "--output-threads", str(threads), "-o", output,
        ]  # fmt: skip
        if regions:
            command += ["--regions", regions[0]]
        return [*command, vcf, reads]
    prefix = output.rsplit(".", 1)[0]
    command = [
        "longphase", "haplotag", "-s", vcf, "-b", reads, "-r", ref,
        "--sample", sample, "-t", str(threads), "-o", prefix,
    ]  # fmt: skip
    if output.endswith(".cram"):
        command.append("--cram")
    if regions:
        command += ["--region", regions[0]]
    return command


def run_phase(params: VcfPhaseParams) -> None:
    """``vcf phase``: phase a VCF with its reads into ``phased.vcf.gz``."""
    require_supported_platform()
    verify_dependencies(["bcftools", "samtools", "tabix"])
    if not params.vcf_input:
        raise UsageError(
            "`vcf phase` needs the VCF to phase: pass --vcf-input (or set "
            "default_input_vcf in config.toml)."
        )
    require_a_phaser(params.phaser)
    vcf = str(params.vcf_input)
    verify_paths_exist({"--vcf-input": vcf})
    current_result().add_input(vcf, "vcf_input")
    setup = setup_caller(params)
    threads, outdir, ref = int(setup.threads), setup.outdir, setup.ref
    reads, region = str(params.input), params.region
    if not alignment_index(reads):
        raise UsageError(
            f"{reads} has no index; phasing reads it by region. Index it with "
            f"`samtools index {reads}`."
        )
    platform = setup.platform
    if platform not in LONG_READ_PLATFORMS:
        warn(
            "short_read_phasing",
            f"The reads look like {platform or 'an unknown platform'}, not "
            "Nanopore or PacBio; read-backed phasing from short reads links few "
            "variants into short blocks.",
            platform=platform or "unknown",
        )
    phaser = choose_phaser(params.phaser, platform)
    log_dependency_info([phaser])
    sample = phase_sample(vcf, get_bam_header(reads, ref))

    out_vcf = str(Path(outdir, "phased.vcf.gz"))
    logging.info(f"Phasing {sample} in {vcf} with {phaser} into {out_vcf}")
    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_phase_") as work:
        prepared = str(Path(work, "input.vcf.gz"))
        prepare_vcf(vcf, sample, region, prepared)
        with safe_output(out_vcf) as staged:
            if phaser == "whatshap":
                run_whatshap(prepared, reads, ref, sample, threads, work, staged)
            else:
                assert platform is not None
                run_longphase(
                    prepared, reads, ref, sample, platform, threads, work, staged
                )
    ensure_vcf_indexed(out_vcf)
    stats = phase_stats(out_vcf)
    blocks_tsv = str(Path(outdir, "phased.blocks.tsv"))
    write_blocks(stats, blocks_tsv)
    metrics: dict[str, Scalar] = {
        "phaser": phaser,
        "heterozygous_variants": stats.heterozygous,
        "phased_variants": stats.phased,
        "phased_pct": stats.phased_pct,
        "phase_blocks": len(stats.blocks),
        "block_n50_bp": stats.block_n50,
    }
    result = current_result()
    result.add_output(out_vcf, "phased_variants", metrics=metrics)
    result.add_output(blocks_tsv, "phase_blocks")
    result.add_metrics(**metrics)
    logging.info(
        f"Phased {stats.phased} of {stats.heterozygous} heterozygous variants "
        f"({stats.phased_pct}%) in {len(stats.blocks)} blocks; block N50 "
        f"{stats.block_n50:,} bp."
    )
    if stats.heterozygous and not stats.phased:
        warn(
            "no_variants_phased",
            "No heterozygous variant was phased: check that the reads and the "
            "VCF are the same sample on the same reference.",
            heterozygous_variants=stats.heterozygous,
        )
    if params.haplotag:
        haplotag(phaser, out_vcf, reads, ref, sample, threads, outdir, region)


def _missing_contig_lines(vcf: str, header: str) -> list[str]:
    """``##contig`` lines for the reads' @SQ contigs that the VCF lacks."""
    out = run_command(["bcftools", "view", "-h", vcf], capture_output=True).stdout
    known = {
        line.split("ID=", 1)[1].split(",", 1)[0].rstrip(">")
        for line in out.splitlines()
        if line.startswith("##contig=<")
    }
    lines = []
    for line in header.splitlines():
        if not line.startswith("@SQ"):
            continue
        tags = dict(f.split(":", 1) for f in line.split("\t")[1:] if ":" in f)
        if tags.get("SN") and tags["SN"] not in known:
            lines.append(f"##contig=<ID={tags['SN']},length={tags.get('LN', 0)}>")
    return lines


def haplotag(
    phaser: str, vcf: str, reads: str, ref: str, sample: str, threads: int,
    outdir: str, region: str | None,
) -> None:  # fmt: skip
    """Tag the reads with HP (haplotype) and PS (phase block)."""
    suffix = ".cram" if reads.lower().endswith(".cram") else ".bam"
    output = str(Path(outdir, f"haplotagged{suffix}"))
    regions = [r.strip() for r in (region or "").split(",") if r.strip()]
    logging.info(f"Tagging reads by haplotype with {phaser} into {output}")
    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_haplotag_") as work:
        # GT and PS only: LongPhase 2.1 takes a FORMAT field such as GIAB's IPS
        # for PS and aborts. Every read contig is declared, as WhatsHap fails on
        # a contig the VCF lacks (a chr20-only VCF with whole-genome reads); its
        # reads are written untagged.
        contigs = str(Path(work, "contigs.txt"))
        with open(contigs, "w", newline="\n") as f:
            f.writelines(
                line + "\n"
                for line in _missing_contig_lines(vcf, get_bam_header(reads, ref))
            )
        slim = str(Path(work, "phased.vcf.gz"))
        run_command(
            ["bcftools", "annotate", "-x", "INFO,^FORMAT/GT,FORMAT/PS",
             "-h", contigs, "-Oz", "-o", slim, vcf]
        )  # fmt: skip
        ensure_vcf_indexed(slim)
        if len(regions) > 1:
            # LongPhase takes one region, and WhatsHap writes a read once per
            # region it overlaps, unsorted; so tag the reads of all of them.
            subset = str(Path(work, "regions.bam"))
            run_command(
                ["samtools", "view", "-M", "-b", "--reference", ref,
                 "-@", str(threads), "-o", subset, reads, *regions]
            )  # fmt: skip
            run_command(["samtools", "index", subset])
            reads, regions = subset, []
        staged = str(Path(work, f"haplotagged{suffix}"))
        run_command(
            haplotag_command(
                phaser, slim, reads, ref, sample, threads, staged, regions
            ),
            env=_tool_env(),
            stdin=subprocess.DEVNULL,
        )
        if not Path(staged).is_file():
            raise WGSExtractError(f"{phaser} haplotag produced no {suffix[1:]} file.")
        with safe_output(output) as final:
            Path(staged).replace(final)
    run_command(["samtools", "index", output])
    current_result().add_output(output, "haplotagged_alignments")


cmd_phase = cli_handler(VcfPhaseParams, run_phase)
