import argparse
import logging
import subprocess
import tempfile
from pathlib import Path

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_assets import input_stem
from wgsextract_cli.core.reference_resolver import (
    ReferenceLibrary,
)
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    popen,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    check_vcf_build,
    chromosome_rename_mapping,
    ensure_vcf_prepared,
    get_vcf_samples,
    match_chromosome_name,
    vcf_index_chromosomes,
    verify_paths_exist,
)

from .basic import require_vcf_input

# --haplotype choice -> bcftools consensus genotype option.
HAPLOTYPE_OPTIONS = {
    "iupac": ["--iupac-codes"],
    "alt": ["--haplotype", "A"],
    "1": ["--haplotype", "1"],
    "2": ["--haplotype", "2"],
}
# Symbolic ALTs gVCF reference blocks use.
_GVCF_REF_ALTS = {".", "<NON_REF>", "<*>"}
_GVCF_PROBE_RECORDS = 2000
_PASSING = 'FILTER="PASS" || FILTER="."'


def is_gvcf(vcf_path: str) -> bool:
    """Whether a VCF has gVCF reference blocks (so absent sites are uncalled)."""
    proc = popen(
        ["bcftools", "view", "-H", vcf_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdout is not None
        for count, line in enumerate(proc.stdout):
            fields = line.split("\t", 8)
            if len(fields) > 7 and "END=" in fields[7]:
                alts = fields[4].split(",")
                if alts[-1] in _GVCF_REF_ALTS:
                    return True
            if count >= _GVCF_PROBE_RECORDS:
                break
        return False
    finally:
        proc.kill()
        proc.wait()


def _reference_lengths(ref: str) -> dict[str, int]:
    fai = ref + ".fai"
    if not Path(fai).is_file():
        run_command(["samtools", "faidx", ref], capture_output=True)
    with open(fai) as f:
        rows = (line.split("\t", 2) for line in f if line.strip())
        return {name: int(length) for name, length, _ in rows}


def _pick_sample(vcf_path: str, sample: str | None) -> str | None:
    samples = get_vcf_samples(vcf_path)
    if sample:
        if sample not in samples:
            raise WGSExtractError(
                f"Sample '{sample}' is not in {vcf_path} (samples: "
                f"{', '.join(samples) or 'none'})."
            )
        return sample
    if len(samples) > 1:
        raise WGSExtractError(
            f"{vcf_path} has {len(samples)} samples; choose one with --sample "
            f"({', '.join(samples)})."
        )
    if not samples:
        raise WGSExtractError(
            f"{vcf_path} has no genotype columns, so a consensus cannot be built."
        )
    return None


def _reference_region(region: str, contigs: list[str]) -> str:
    """Rewrite a region's contig (e.g. 21 or MT) to the reference's naming."""
    contig, sep, span = region.partition(":")
    matched = match_chromosome_name(contig, contigs)
    if not matched:
        raise WGSExtractError(f"Region contig '{contig}' is not in the reference.")
    return f"{matched}{sep}{span}"


def _prepare_vcf(
    vcf: str,
    contigs: list[str],
    region: str | None,
    sample: str | None,
    gvcf: bool,
    threads: str,
    workdir: str,
) -> str:
    """One streaming pass that subsets to the region/sample, drops contigs the
    reference lacks, trims unused ALT alleles and renames contigs to the
    reference's naming, as needed.

    Trimming turns gVCF reference blocks (ALT ``<NON_REF>``/``<*>``) into ALT
    ``.``, the only form ``bcftools consensus`` accepts, and drops the unused
    symbolic allele from variant records.
    """
    vcf_contigs = vcf_index_chromosomes(vcf)
    mapping = chromosome_rename_mapping(vcf_contigs, contigs)

    view = ["bcftools", "view", "--threads", threads]
    if region:
        contig, sep, span = region.partition(":")
        vcf_contig = match_chromosome_name(contig, vcf_contigs) or contig
        view += ["--regions", f"{vcf_contig}{sep}{span}"]
    else:
        shared = [c for c in vcf_contigs if match_chromosome_name(c, contigs)]
        if not shared:
            raise WGSExtractError("None of the VCF's contigs are in the reference.")
        if len(shared) < len(vcf_contigs):
            skipped = sorted(set(vcf_contigs) - set(shared))
            logging.warning(
                "Skipping %d VCF contig(s) not in the reference: %s",
                len(skipped),
                ", ".join(skipped[:10]) + (" ..." if len(skipped) > 10 else ""),
            )
            # A file, as names like HLA-A*01:01:01:01 break comma region lists.
            regions_file = str(Path(workdir, "contigs.tsv"))
            with open(regions_file, "w") as f:
                f.write("".join(f"{c}\t1\t{2**31 - 1}\n" for c in shared))
            view += ["--regions-file", regions_file]
    if not (mapping or gvcf or "--regions" in view or "--regions-file" in view):
        return vcf
    if sample:
        view += ["--samples", sample]
    if gvcf:
        view.append("--trim-alt-alleles")
    prepared = str(Path(workdir, "prepared.bcf"))
    output = ["--threads", threads, "-Ob", "-o", prepared, "--write-index"]
    if mapping:
        map_file = str(Path(workdir, "contigs.txt"))
        with open(map_file, "w") as f:
            f.write("".join(f"{src} {dst}\n" for src, dst in mapping))
        rename = ["bcftools", "annotate", "--rename-chrs", map_file, *output, "-"]
        stages = [view + ["-Ou", vcf], rename]
    else:
        stages = [view + [*output, vcf]]
    logging.info("Preparing VCF records for consensus...")
    run_pipeline(stages, description="VCF preparation")
    return prepared


def _fixed_ref_record(line: str, fasta_ref: str) -> str | None:
    """The record with REF set to the FASTA's bases where REF has N, or None
    when no such fix makes REF match (a genuine mismatch is left to fail)."""
    fields = line.rstrip("\n").split("\t")
    old = fields[3].upper()
    new = fasta_ref.upper()
    if old == new or len(old) != len(new):
        return None
    if any(o not in ("N", f) for o, f in zip(old, new, strict=True)):
        return None
    fields[3] = fasta_ref
    fields[4] = ",".join(
        fasta_ref[0] + alt[1:] if alt[:1].upper() == old[0] else alt
        for alt in fields[4].split(",")
    )
    return "\t".join(fields) + "\n"


def _match_ambiguous_ref_bases(vcf: str, ref: str, threads: str, workdir: str) -> str:
    """Set N REF bases to the reference's IUPAC codes, so consensus accepts them.

    Callers write ``N`` where the reference has an ambiguity code (e.g. ``R``
    at hs38 chr7:154575459), but ``bcftools consensus`` requires REF to equal
    the FASTA exactly; ``bcftools norm --check-ref s`` writes a resolved base
    (``A``) instead, which consensus rejects too. Such records are rare (a
    reference has ~100 IUPAC bases), so only they are rewritten and merged back.
    """
    candidates = '(REF~"[^ACGTacgt]") && ALT!="."'
    records = run_command(
        ["bcftools", "view", "-H", "--include", candidates, vcf], capture_output=True
    ).stdout.splitlines(keepends=True)
    if not records:
        return vcf
    regions = []
    for line in records:
        chrom, pos, _, ref_allele = line.split("\t", 4)[:4]
        regions.append(f"{{{chrom}}}:{pos}-{int(pos) + len(ref_allele) - 1}")
    fasta = run_command(["samtools", "faidx", ref, *regions], capture_output=True)
    bases = ["".join(s.splitlines()[1:]) for s in fasta.stdout.split(">")[1:]]
    fixes = [
        _fixed_ref_record(line, seq) for line, seq in zip(records, bases, strict=True)
    ]
    if not any(fixes):
        return vcf
    logging.info(
        "Setting REF to the reference's ambiguity code at %d site(s).",
        sum(1 for fix in fixes if fix),
    )
    header = run_command(["bcftools", "view", "-h", vcf], capture_output=True).stdout
    fixed_vcf = str(Path(workdir, "ambiguous.vcf"))
    with open(fixed_vcf, "w") as f:
        f.write(header)
        f.writelines(fix or line for line, fix in zip(records, fixes, strict=True))
    fixed_bcf = str(Path(workdir, "ambiguous.bcf"))
    rest = str(Path(workdir, "unambiguous.bcf"))
    out = str(Path(workdir, "prepared_fixed.bcf"))
    write = ["--threads", threads, "-Ob", "--write-index", "-o"]
    for cmd in (
        ["bcftools", "view", *write, fixed_bcf, fixed_vcf],
        ["bcftools", "view", "--exclude", candidates, *write, rest, vcf],
        ["bcftools", "concat", "--allow-overlaps", *write, out, rest, fixed_bcf],
    ):
        run_command(cmd, capture_output=True)
    return out


def _region_bounds(region: str, lengths: dict[str, int]) -> tuple[str, int, int]:
    """(contig, 0-based start, end) of a samtools-style region."""
    contig, _, span = region.partition(":")
    start, _, end = span.replace(",", "").partition("-")
    return contig, max(int(start or 1) - 1, 0), int(end or lengths[contig])


def write_uncalled_mask(
    vcf: str,
    lengths: dict[str, int],
    out_bed: str,
    region: str | None = None,
    include: str | None = None,
) -> None:
    """BED of reference positions no (included) VCF record covers.

    Used instead of ``bcftools consensus --absent``, which is quadratic in the
    length of uncalled stretches and stalls for hours on centromeres.
    """
    if region:
        contig, lo, hi = _region_bounds(region, lengths)
        bounds = {contig: (lo, hi)}
    else:
        bounds = {name: (0, length) for name, length in lengths.items()}
    cmd = ["bcftools", "query", "-f", "%CHROM\t%POS0\t%END\n"]
    cmd += (["--include", include] if include else []) + [vcf]

    seen: set[str] = set()
    with tempfile.TemporaryFile() as err, open(out_bed, "w") as bed:

        def gap(chrom: str, beg: int, end: int) -> None:
            lo, hi = bounds[chrom]
            beg, end = max(beg, lo), min(end, hi)
            if beg < end:
                bed.write(f"{chrom}\t{beg}\t{end}\n")

        proc = popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        assert proc.stdout is not None
        chrom, covered = "", 0
        for line in proc.stdout:
            name, beg, end = line.split("\t")
            if name != chrom:
                if chrom in bounds:
                    gap(chrom, covered, bounds[chrom][1])
                chrom, covered = name, bounds.get(name, (0, 0))[0]
                seen.add(name)
            if chrom in bounds:
                gap(chrom, covered, int(beg))
                covered = max(covered, int(end))
        if chrom in bounds:
            gap(chrom, covered, bounds[chrom][1])
        if proc.wait() != 0:
            err.seek(0)
            raise WGSExtractError(
                "Listing called positions failed: "
                + err.read().decode(errors="replace").strip()[-2000:]
            )
        # Contigs with no records at all are wholly uncalled.
        for name, (lo, hi) in bounds.items():
            if name not in seen and lo < hi:
                bed.write(f"{name}\t{lo}\t{hi}\n")


def cmd_to_fasta(args: argparse.Namespace) -> None:
    tools = ["bcftools", "samtools", "bgzip", "tabix"]
    verify_dependencies(tools)
    log_dependency_info(tools)

    input_vcf = require_vcf_input(args)
    verify_paths_exist({"--input": input_vcf})
    if args.mask:
        verify_paths_exist({"--mask": args.mask})
    threads, _ = get_resource_defaults(args.threads, None)
    outdir = args.outdir or str(absolute_path(input_vcf).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)

    ref = ReferenceLibrary(args.ref, None, input_path=input_vcf).fasta
    if not ref or not Path(ref).is_file():
        raise WGSExtractError(
            LOG_MESSAGES["ref_required_for"].format(task="consensus generation")
        )
    verify_paths_exist({"--ref": ref})

    stem = input_stem(input_vcf)
    out_fasta = str(Path(outdir, f"{stem}_consensus.fa.gz"))
    out_chain = str(Path(outdir, f"{stem}_consensus.chain")) if args.chain else None
    input_vcf = ensure_vcf_prepared(input_vcf)
    sample = _pick_sample(input_vcf, args.sample)

    gvcf = is_gvcf(input_vcf)
    uncalled = args.uncalled
    if uncalled == "auto":
        uncalled = "n" if gvcf else "ref"
        logging.info(
            "Uncalled positions will be %s (%s).",
            "masked with N" if uncalled == "n" else "left as the reference base",
            "gVCF input" if uncalled == "n" else "variant-only VCF input",
        )

    lengths = _reference_lengths(ref)
    check_vcf_build(input_vcf, lengths, "--ref")
    contigs = list(lengths)
    region = _reference_region(args.region, contigs) if args.region else None
    include = None if args.include_filtered else _PASSING
    consensus_cmd = ["bcftools", "consensus", *HAPLOTYPE_OPTIONS[args.haplotype]]
    if sample:
        consensus_cmd += ["--samples", sample]
    if include:
        consensus_cmd += ["--include", include]
    if uncalled == "n":
        consensus_cmd += ["--missing", "N"]
    if args.mask:
        consensus_cmd += ["--mask", args.mask]
    if out_chain:
        consensus_cmd += ["--chain", out_chain]

    logging.info(
        LOG_MESSAGES["generating_consensus"].format(output=out_fasta)
        + (f" (region {region})" if region else "")
    )
    partials = [out_fasta, out_fasta + ".fai", out_fasta + ".gzi"]
    # Only remove outputs this run started writing, so a failure while
    # preparing inputs leaves an earlier result intact.
    writing = False
    try:
        with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_consensus_") as w:
            vcf = _prepare_vcf(input_vcf, contigs, region, sample, gvcf, threads, w)
            vcf = _match_ambiguous_ref_bases(vcf, ref, threads, w)
            if uncalled == "n":
                uncalled_bed = str(Path(w, "uncalled.bed"))
                logging.info("Finding positions the VCF does not cover...")
                write_uncalled_mask(vcf, lengths, uncalled_bed, region, include)
                consensus_cmd += ["--mask", uncalled_bed]
            stages = [consensus_cmd + ["--fasta-ref", ref, vcf]]
            if region:
                stages = [
                    ["samtools", "faidx", ref, region],
                    consensus_cmd + ["--fasta-ref", "-", vcf],
                ]
            stages.append(["bgzip", "--threads", threads, "-c"])
            writing = True
            with open(out_fasta, "wb") as f:
                run_pipeline(stages, stdout=f, description="Consensus generation")
        run_command(["samtools", "faidx", out_fasta], capture_output=True)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        outputs = partials + ([out_chain] if out_chain else [])
        for path in outputs if writing else []:
            if Path(path).exists():
                Path(path).unlink()
        raise WGSExtractError(f"Consensus generation failed: {e}") from e

    logging.info(f"Consensus FASTA written to {out_fasta}")
    if out_chain:
        logging.info(f"Liftover chain (reference -> consensus) written to {out_chain}")
