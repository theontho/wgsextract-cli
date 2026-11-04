"""`methylation`: base modifications (5mC/5hmC) from long-read MM/ML tags.

`methylation pileup` runs ONT's modkit over a BAM/CRAM whose reads carry
SAM base-modification tags (MM/ML, written by Dorado for Nanopore, and by
Jasmine/primrose for PacBio HiFi) and writes a bedMethyl table with a short
summary. modkit is an optional tool from its own Pixi environment; it is
checked, never installed.
"""

import argparse
import gzip
import logging
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from wgsextract_cli.commands.vcf.basic import plain_reference
from wgsextract_cli.core.atomic_output import safe_output
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_assets import input_stem, read_fai_lengths
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.results import current_result
from wgsextract_cli.core.samtools_commands import alignment_index
from wgsextract_cli.core.sequencing import input_platform
from wgsextract_cli.core.table_writer import (
    Column,
    TableWriter,
    add_format_argument,
    arrow_schema,
    check_format,
    provenance,
    require_pyarrow,
    table_path,
    write_table,
)
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    get_resource_defaults,
    popen,
    run_command,
)
from wgsextract_cli.core.variant_files import calculate_bam_md5, verify_paths_exist

MODKIT = "modkit"
CONTEXTS = ("cpg", "all")
WINDOWS_UNSUPPORTED = (
    "modkit is not available on Windows (Bioconda and ONT build it for Linux "
    "and macOS only). Run `methylation pileup` on Linux or macOS, for example "
    "under WSL2."
)
# Long-read platforms whose basecallers write MM/ML tags.
MODIFICATION_PLATFORMS = ("ont", "pacbio")
# Reads sampled to decide whether the input carries modification tags.
TAG_SAMPLE_READS = 200
# Below this share of tagged reads the output covers only part of the data.
MIN_TAGGED_SHARE = 0.5
# Sites with fewer valid calls than this are left out of the summary.
SUMMARY_MIN_COVERAGE = 5
MOD_NAMES = {"m": "5mC", "h": "5hmC"}


def require_supported_platform() -> None:
    if sys.platform == "win32":
        raise WGSExtractError(WINDOWS_UNSUPPORTED)


def _setting(value: object, key: str, default: object) -> object:
    """A CLI value, else config.toml's, else the default."""
    return value if value is not None else settings.get(key, default)


def _bool_setting(value: bool | None, key: str, default: bool) -> bool:
    chosen = _setting(value, key, default)
    if not isinstance(chosen, bool):
        raise WGSExtractError(f"{key} in config.toml must be true or false.")
    return chosen


@dataclass(frozen=True)
class PileupOptions:
    context: str = "cpg"
    hydroxymethyl: bool = False
    combine_strands: bool = True

    @property
    def modified_bases(self) -> list[str]:
        return ["5mC", "5hmC"] if self.hydroxymethyl else ["5mC"]


def pileup_options(args: argparse.Namespace) -> PileupOptions:
    """--context/--5hmc/--combine-strands, then config.toml, then defaults."""
    context = str(
        _setting(getattr(args, "context", None), "methylation_context", "cpg")
    )
    if context not in CONTEXTS:
        raise WGSExtractError(
            f"methylation_context must be one of {', '.join(CONTEXTS)}; got {context}."
        )
    hydroxymethyl = _bool_setting(
        getattr(args, "hydroxymethyl", None), "methylation_5hmc", False
    )
    explicit = getattr(args, "combine_strands", None)
    combine = _bool_setting(explicit, "methylation_combine_strands", True)
    if combine and context != "cpg":
        # modkit sums strands only at palindromic motifs such as CpG.
        if explicit:
            raise WGSExtractError(
                "--combine-strands needs --context cpg: strands can only be summed "
                "at palindromic motifs."
            )
        combine = False
    return PileupOptions(context, hydroxymethyl, combine)


def region_intervals(
    region: str, lengths: dict[str, int]
) -> list[tuple[str, int, int]]:
    """chr1:1000-1050,chr2 (1-based, inclusive) as 0-based, half-open intervals."""
    intervals = []
    for part in (r.strip() for r in region.split(",") if r.strip()):
        contig, _, span = part.partition(":")
        if contig not in lengths:
            raise WGSExtractError(f"{contig} isn't in the reference.")
        start, _, end = span.partition("-")
        try:
            first = int(start) - 1 if start else 0
            last = int(end) if end else (int(start) if start else lengths[contig])
        except ValueError as error:
            raise WGSExtractError(f"Invalid region {part}.") from error
        if not 0 <= first < last <= lengths[contig]:
            raise WGSExtractError(f"Region {part} is outside {contig}.")
        intervals.append((contig, first, last))
    if not intervals:
        raise WGSExtractError(f"Invalid region {region!r}.")
    return intervals


def modkit_command(
    reads: str,
    output: str,
    ref: str,
    options: PileupOptions,
    threads: str,
    intervals: list[tuple[str, int, int]] | None = None,
    bed: str | None = None,
) -> list[str]:
    """modkit pileup writing a BGZF bedMethyl. Its --region is 0-based, half-open."""
    io_threads = str(min(int(threads), 4))
    command = [
        MODKIT,
        "pileup",
        reads,
        output,
        "--ref",
        ref,
        "--modified-bases",
        *options.modified_bases,
        "--threads",
        threads,
        "--io-threads",
        io_threads,
        "--bgzf",
        "--bgzf-threads",
        io_threads,
        "--suppress-progress",
    ]
    if options.context == "cpg":
        command.append("--cpg")
    if options.combine_strands:
        command.append("--combine-strands")
    if intervals and len(intervals) == 1:
        contig, start, end = intervals[0]
        command += ["--region", f"{contig}:{start}-{end}"]
    elif bed:
        command += ["--include-bed", bed]
    return command


@dataclass(frozen=True)
class TagSample:
    reads: int
    tagged: int
    # Reads with MM entries for the opposite strand (G-h?, T-a., G-m?).
    opposite_strand: int = 0
    # Dorado duplex reads (dx:i:1), one cause of opposite-strand entries.
    duplex: int = 0


def _has_opposite_strand(mm: str) -> bool:
    return any(entry[1:2] == "-" for entry in mm[5:].split(";") if entry)


def sample_modification_tags(
    alignment: str,
    ref: str | None,
    regions: Iterable[str] = (),
    limit: int = TAG_SAMPLE_READS,
) -> TagSample:
    """How many of the first primary, mapped reads carry MM and ML tags."""
    cram = ["-T", ref] if ref else []
    command = ["samtools", "view", "-F", "0x904", *cram, alignment, *regions]
    reads = tagged = opposite = duplex = 0
    stopped = False
    with tempfile.TemporaryFile("w+") as errors:
        process = popen(command, stdout=subprocess.PIPE, stderr=errors, text=True)
        assert process.stdout is not None
        try:
            for line in process.stdout:
                tags = line.rstrip("\n").split("\t")[11:]
                # The SAM spec's old Mm/Ml names are still written by some tools.
                names = {tag[:2] for tag in tags}
                reads += 1
                if names & {"MM", "Mm"} and names & {"ML", "Ml"}:
                    tagged += 1
                    opposite += any(
                        tag[:2] in ("MM", "Mm") and _has_opposite_strand(tag)
                        for tag in tags
                    )
                    duplex += "dx:i:1" in tags
                if reads >= limit:
                    stopped = True
                    break
        finally:
            process.stdout.close()
            if stopped and process.poll() is None:
                process.terminate()
            returncode = process.wait()
        if returncode and not stopped:
            errors.seek(0)
            raise WGSExtractError(
                f"samtools could not read {alignment}: "
                f"{errors.read().strip() or f'exit code {returncode}'}"
            )
    return TagSample(reads, tagged, opposite, duplex)


def require_modification_platform(platform: str | None) -> None:
    """Long reads only: short-read methylation (bisulfite, EM-seq) needs other tools."""
    if platform is None or platform in MODIFICATION_PLATFORMS:
        return
    raise WGSExtractError(
        f"The input looks like {platform} short reads. `methylation pileup` "
        "summarizes base modifications that Nanopore and PacBio basecallers write "
        "as MM/ML tags; short reads carry none. Bisulfite or EM-seq data needs a "
        "conversion-aware aligner such as Bismark instead."
    )


def check_modification_tags(sample: TagSample, platform: str | None) -> None:
    """Refuse reads without MM/ML tags; warn when only some carry them."""
    if sample.reads == 0:
        raise WGSExtractError(
            "No mapped reads in the input (or the region) to tabulate methylation from."
        )
    if sample.tagged == 0:
        if platform == "ont":
            reason = (
                "Dorado writes them only with a modified-base model (for example "
                "--modified-bases 5mCG_5hmCG), and they are lost when reads are "
                "realigned through FASTQ without keeping them (samtools fastq -T "
                "MM,ML,MN, minimap2 -y). `wgsextract realign` keeps them; realign "
                "from the original unaligned BAM."
            )
        elif platform == "pacbio":
            reason = (
                "PacBio HiFi BAMs carry them when 5mC calling (Jasmine or primrose) "
                "was run, and they must survive alignment (pbmm2 keeps them)."
            )
        else:
            reason = (
                "Only Nanopore (Dorado with a modified-base model) and PacBio HiFi "
                "(Jasmine/primrose) reads carry them; short reads never do."
            )
        raise WGSExtractError(
            f"None of {sample.reads} sampled reads carries MM/ML base-modification "
            f"tags, so there is no methylation to tabulate. {reason}"
        )
    share = sample.tagged / sample.reads
    if share < MIN_TAGGED_SHARE:
        logging.warning(
            f"Only {sample.tagged} of {sample.reads} sampled reads carry MM/ML tags; "
            "methylation will come from those reads alone, at lower coverage."
        )
    if not sample.opposite_strand:
        return
    # modkit 0.6's pileup skips any read with an opposite-strand MM entry.
    if platform == "pacbio":
        source = (
            "Jasmine writes them for 5hmC (G-h?) and 6mA (T-a.), which are on by "
            "default. Re-run Jasmine with only its 5mC model (see jasmine --help); "
            "older primrose or ccs-kinetics 5mC output (C+m? only) works as is."
        )
    elif platform == "ont" or sample.duplex:
        source = (
            "Dorado writes them for duplex reads (dx:i:1). Use the simplex reads "
            "Dorado writes alongside them, or run modkit pileup-hemi directly."
        )
    else:
        source = "Remove those modification calls, or run modkit pileup-hemi directly."
    if sample.opposite_strand == sample.tagged:
        raise WGSExtractError(
            f"All {sample.tagged} sampled reads with MM/ML tags carry "
            "opposite-strand modification calls, and modkit pileup skips such "
            f"reads entirely. {source}"
        )
    logging.warning(
        f"{sample.opposite_strand} of {sample.tagged} sampled reads with MM/ML tags "
        "carry opposite-strand modification calls; modkit pileup skips them, so "
        f"coverage comes from the other reads. {source}"
    )


@dataclass(frozen=True)
class ModSummary:
    modification: str
    sites: int
    summary_sites: int
    median_coverage: float
    weighted_percent: float
    mean_site_percent: float
    high_sites_percent: float
    low_sites_percent: float

    def row(self) -> tuple[str, int, int, float, float, float, float, float]:
        return (
            self.modification,
            self.sites,
            self.summary_sites,
            self.median_coverage,
            self.weighted_percent,
            self.mean_site_percent,
            self.high_sites_percent,
            self.low_sites_percent,
        )


_PERCENT = {"type": "REAL", "spec": ".2f"}
SUMMARY_TABLE = (
    Column("modification", "TEXT", "Modification (5mC, 5hmC, ...)."),
    Column("sites", "INTEGER", "Sites with at least one valid call."),
    Column(
        f"sites_coverage_ge_{SUMMARY_MIN_COVERAGE}",
        "INTEGER",
        f"Sites with at least {SUMMARY_MIN_COVERAGE} valid calls, which the "
        "remaining columns describe.",
    ),
    Column("median_coverage", "REAL", "Median valid calls per site.", "g"),
    Column(
        "weighted_percent_modified",
        description="Modified calls as a percentage of all valid calls.",
        **_PERCENT,
    ),
    Column(
        "mean_site_percent_modified",
        description="Mean of the sites' percentages modified.",
        **_PERCENT,
    ),
    Column(
        "percent_sites_ge_80", description="Sites at least 80% modified.", **_PERCENT
    ),
    Column(
        "percent_sites_le_20", description="Sites at most 20% modified.", **_PERCENT
    ),
)
SUMMARY_COLUMNS = [column.name for column in SUMMARY_TABLE]

# modkit's bedMethyl columns, without the display-only 7-9 (thickStart,
# thickEnd, color), which repeat start, end and a constant.
BEDMETHYL_TABLE = (
    Column("chrom", "TEXT", "Contig."),
    Column("start", "INTEGER", "0-based start of the site."),
    Column("end", "INTEGER", "0-based, exclusive end of the site."),
    Column("mod_code", "TEXT", "Modification code (m: 5mC, h: 5hmC, ...)."),
    Column("score", "INTEGER", "modkit's score: the valid coverage."),
    Column("strand", "TEXT", "+, - or . (strands combined)."),
    Column("n_valid_cov", "INTEGER", "Valid calls: modified + canonical + other."),
    Column("percent_modified", "REAL", "n_mod as a percentage of n_valid_cov."),
    Column("n_mod", "INTEGER", "Calls of this modification."),
    Column("n_canonical", "INTEGER", "Unmodified calls."),
    Column("n_other_mod", "INTEGER", "Calls of another modification."),
    Column("n_delete", "INTEGER", "Reads with a deletion here."),
    Column("n_fail", "INTEGER", "Calls below modkit's confidence threshold."),
    Column("n_diff", "INTEGER", "Reads with another base here."),
    Column("n_nocall", "INTEGER", "Reads without a modification call here."),
)
_BEDMETHYL_FIELDS = (1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, 15, 16, 17, 18)


@dataclass
class _ModTally:
    sites: int = 0
    calls: int = 0
    modified: int = 0
    percent_sum: float = 0.0
    high: int = 0
    low: int = 0
    # Coverage -> site count, for an exact median in constant memory.
    coverages: Counter[int] = field(default_factory=Counter)

    def add(self, coverage: int, modified: int, percent: float) -> None:
        self.calls += coverage
        self.modified += modified
        self.percent_sum += percent
        self.high += percent >= 80
        self.low += percent <= 20
        self.coverages[coverage] += 1

    def summary(self, code: str) -> ModSummary:
        n = self.coverages.total()
        return ModSummary(
            modification=MOD_NAMES.get(code, code),
            sites=self.sites,
            summary_sites=n,
            median_coverage=_counter_median(self.coverages),
            weighted_percent=100 * self.modified / self.calls if self.calls else 0.0,
            mean_site_percent=self.percent_sum / n if n else 0.0,
            high_sites_percent=100 * self.high / n if n else 0.0,
            low_sites_percent=100 * self.low / n if n else 0.0,
        )


def _counter_median(counts: Counter[int]) -> float:
    n = counts.total()
    lower, upper = (n - 1) // 2, n // 2
    seen = 0
    low: int | None = None
    for value in sorted(counts):
        seen += counts[value]
        if low is None and seen > lower:
            low = value
        if seen > upper:
            assert low is not None
            return (low + value) / 2
    return 0


def summarize_bedmethyl(
    path: str, min_coverage: int = SUMMARY_MIN_COVERAGE
) -> list[ModSummary]:
    """Per modification: weighted (pooled calls) and per-site methylation levels.

    Streams the table, so whole-genome all-context output fits in memory.
    bedMethyl columns: 4 modification code, 10 valid coverage, 11 percent
    modified, 12 modified calls.
    """
    tallies: dict[str, _ModTally] = {}
    opener = gzip.open if path.endswith((".gz", ".bgz")) else open
    with opener(path, "rt") as handle:
        for line in handle:
            fields = line.split("\t")
            if len(fields) < 12:
                continue
            tally = tallies.setdefault(fields[3], _ModTally())
            tally.sites += 1
            coverage = int(fields[9])
            if coverage >= min_coverage:
                tally.add(coverage, int(fields[11]), float(fields[10]))
    order = list(MOD_NAMES)
    codes = sorted(
        tallies, key=lambda c: (order.index(c) if c in order else len(order), c)
    )
    return [tallies[code].summary(code) for code in codes]


def write_summary(
    path: str,
    summaries: list[ModSummary],
    table_format: str = "tsv",
    metadata: dict[str, str] | None = None,
) -> None:
    rows = (summary.row() for summary in summaries)
    write_table(path, SUMMARY_TABLE, rows, table_format, metadata=metadata)


def bedmethyl_to_parquet(bed: str, path: str, metadata: dict[str, str]) -> int:
    """Convert modkit's bgzipped bedMethyl to typed Parquet, block by block,
    so tens of millions of sites never sit in memory at once."""
    require_pyarrow()
    import pyarrow as pa
    import pyarrow.csv as pacsv

    with pa.input_stream(bed, compression="detect") as stream:
        empty = not stream.read(1)
    if empty:  # Arrow's CSV reader rejects an empty file.
        with TableWriter(path, BEDMETHYL_TABLE, "parquet", metadata=metadata):
            pass
        return 0
    names = [f"f{i}" for i in range(1, 19)]
    keep = [f"f{i}" for i in _BEDMETHYL_FIELDS]
    reader = pacsv.open_csv(
        bed,
        # Small blocks keep memory low (32 MB blocks peaked near 2 GB on 31M
        # sites) and are no slower; write_arrow gathers them into row groups.
        read_options=pacsv.ReadOptions(column_names=names, block_size=4 << 20),
        parse_options=pacsv.ParseOptions(delimiter="\t", quote_char=False),
        convert_options=pacsv.ConvertOptions(
            include_columns=keep,
            column_types={
                name: field.type
                for name, field in zip(keep, arrow_schema(BEDMETHYL_TABLE), strict=True)
            },
        ),
    )
    with TableWriter(path, BEDMETHYL_TABLE, "parquet", metadata=metadata) as writer:
        for batch in reader:
            writer.write_arrow(batch)
    return writer.rows


def index_fasta(fasta: str) -> None:
    """modkit and --region both need the reference's .fai."""
    if Path(f"{fasta}.fai").is_file():
        return
    try:
        run_command(["samtools", "faidx", fasta], capture_output=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(
            f"Could not index {fasta}: {e}. Run samtools faidx on it first."
        ) from e


def cmd_pileup(args: argparse.Namespace) -> None:
    require_supported_platform()
    options = pileup_options(args)
    if not args.input:
        raise UsageError("--input (a BAM or CRAM file) is required.")
    if not args.input.lower().endswith((".bam", ".cram")):
        raise WGSExtractError("--input must be a coordinate-sorted BAM or CRAM.")
    verify_paths_exist({"--input": args.input})
    verify_dependencies([MODKIT, "samtools", "tabix"])
    check_format(args.table_format)
    log_dependency_info([MODKIT])
    if not alignment_index(args.input):
        raise WGSExtractError(
            f"{args.input} needs an index (.bai/.crai): wgsextract bam index -i "
            f"{args.input}"
        )
    threads, _memory = get_resource_defaults(args.threads, None)
    outdir = args.outdir or str(absolute_path(args.input).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    lib = ReferenceLibrary(
        args.ref, calculate_bam_md5(args.input, None), input_path=args.input
    )
    ref = lib.fasta
    if not ref or not Path(ref).is_file():
        raise WGSExtractError(
            "--ref is required: the reference FASTA the reads were aligned to "
            "(modkit reads CpG context from it)."
        )

    platform = input_platform(args.input, ref)
    require_modification_platform(platform)
    if platform == "pacbio" and options.hydroxymethyl:
        logging.warning(
            "Jasmine records most PacBio 5hmC calls as opposite-strand (G-h?) "
            "entries, which modkit cannot use; --5hmc reports only 5hmC on reads "
            "without them, if any."
        )
    region = getattr(args, "region", None)
    regions = [r.strip() for r in region.split(",") if r.strip()] if region else []
    check_modification_tags(
        sample_modification_tags(args.input, ref, regions), platform
    )
    if platform is None:
        logging.warning(
            "Could not tell the sequencing platform; tabulating the MM/ML tags as "
            "they are. Only Nanopore and PacBio HiFi data are supported."
        )

    stem = input_stem(args.input)
    out_bed = str(Path(outdir, f"{stem}.{options.context}.bedmethyl.gz"))
    out_summary = table_path(
        Path(outdir, f"{stem}.{options.context}.methylation"), args.table_format
    )
    logging.info(f"Tabulating base modifications with modkit to {out_bed}")
    with (
        plain_reference(ref, outdir) as use_ref,
        tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_modkit_") as work,
    ):
        index_fasta(use_ref)
        intervals = bed = None
        if region:
            intervals = region_intervals(region, read_fai_lengths(use_ref))
            bed = str(Path(work, "regions.bed"))
            with open(bed, "w", newline="\n") as f:
                f.writelines(f"{c}\t{s}\t{e}\n" for c, s, e in intervals)
        with safe_output(out_bed) as staged:
            command = modkit_command(
                args.input, staged, use_ref, options, threads, intervals, bed
            )
            try:
                run_command(command)
            except (OSError, subprocess.SubprocessError) as e:
                raise WGSExtractError(f"modkit pileup failed: {e}") from e
    run_command(["tabix", "-f", "-p", "bed", out_bed])
    result = current_result()
    result.set_reference(ref, lib.build)
    result.add_output(out_bed, "bedmethyl", kind="bed")

    metadata = provenance(
        [args.input],
        reference_build=lib.build,
        sorted_by="modkit's output order (chrom, start)",
        context=options.context,
        region=region or "",
    )
    summaries = summarize_bedmethyl(out_bed)
    write_summary(out_summary, summaries, args.table_format, metadata)
    result.add_output(out_summary, "methylation_summary")
    if args.table_format == "parquet":
        out_table = str(Path(outdir, f"{stem}.{options.context}.bedmethyl.parquet"))
        logging.info(f"Writing the sites as Parquet to {out_table}")
        sites = bedmethyl_to_parquet(out_bed, out_table, metadata)
        result.add_output(out_table, "bedmethyl_table", metrics={"rows": sites})
    if not any(summary.summary_sites for summary in summaries):
        logging.warning(
            f"No site reached {SUMMARY_MIN_COVERAGE} valid calls; the coverage is "
            "too low for methylation levels."
        )
    for summary in summaries:
        logging.info(
            f"{summary.modification}: {summary.weighted_percent:.1f}% modified "
            f"across {summary.summary_sites:,} sites with >= "
            f"{SUMMARY_MIN_COVERAGE} calls (median coverage "
            f"{summary.median_coverage:g}); {summary.high_sites_percent:.1f}% of "
            f"sites >= 80%, {summary.low_sites_percent:.1f}% <= 20%."
        )
    logging.info(f"Summary written to {out_summary}")


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser("methylation", help=CLI_HELP["cmd_methylation"])
    subs = parser.add_subparsers(dest="methylation_cmd", required=True)

    pileup = subs.add_parser(
        "pileup", parents=[base_parser], help=CLI_HELP["cmd_methylation-pileup"]
    )
    pileup.add_argument(
        "-r",
        "--region",
        help="Region(s), 1-based and inclusive: chr20:10000000-11000000 or "
        "chr1,chr2 (default: the whole genome)",
    )
    pileup.add_argument(
        "--context",
        choices=CONTEXTS,
        default=None,
        help="cpg: CpG sites only; all: every cytosine (CHG/CHH too). Overrides "
        "methylation_context in config.toml (default: cpg).",
    )
    pileup.add_argument(
        "--5hmc",
        dest="hydroxymethyl",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Also report 5-hydroxymethylcytosine (Nanopore 5mCG_5hmCG models). "
        "Overrides methylation_5hmc in config.toml (default: 5mC only).",
    )
    pileup.add_argument(
        "--combine-strands",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Sum both strands of each CpG into one row on its C. Overrides "
        "methylation_combine_strands in config.toml (default: on for CpG).",
    )
    add_format_argument(
        pileup,
        help="Summary table format: tsv, or parquet (needs pyarrow), which also "
        "writes the sites as <stem>.<context>.bedmethyl.parquet",
    )
    pileup.set_defaults(func=cmd_pileup)
