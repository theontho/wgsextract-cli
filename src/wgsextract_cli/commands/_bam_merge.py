"""``bam merge``: lanes or flow-cell runs of one sample into one file (#339).

Each run keeps its read group; the read groups get one sample name (SM) and
platform (PL). samtools merge names the read groups (renaming clashing IDs and,
with -r, naming them after the files); when their lines need SM or PL, the
merged header is worked out beforehand by merging the inputs' headers alone,
and swapped in with samtools reheader as the reads stream through.
"""

import logging
import re
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from os import fspath
from pathlib import Path

from wgsextract_cli.api.bam import BamMergeParams
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.output_checks import record_alignment_output
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.results import current_result, human_output, warn
from wgsextract_cli.core.sequencing import platform_from_metadata
from wgsextract_cli.core.ultima import read_groups
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    get_resource_defaults,
    popen,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    is_sorted,
    resolve_reference,
    verify_paths_exist,
)
from wgsextract_cli.core.warnings import check_free_space

from ._extract_helpers import cleanup_files, named_output_path

# Primary alignments read from the start of each input to look for reads that
# two inputs share and for modified-base tags.
PROBE_READS = 5000
# SAM's PL values for the platforms that sequencing.py detects.
PLATFORM_TAGS = {
    "ont": "ONT",
    "pacbio": "PACBIO",
    "illumina": "ILLUMINA",
    "dnbseq": "DNBSEQ",
    "ultima": "ULTIMA",
}
LONG_READ_PLATFORMS = {"ont", "pacbio"}
_COMPLEMENT = str.maketrans("ACGTNacgtn", "TGCANtgcan")
# samtools merge's seed for the suffixes of clashing read-group IDs, fixed so
# the header-only dry run names them as the real merge does.
MERGE_SEED = "339"


@dataclass
class _Probe:
    """The first primary alignments of an input."""

    # (name, which mate) -> hash of the read's bases as sequenced
    reads: dict[tuple[str, int], int] = field(default_factory=dict)
    modified: int = 0
    # Reads without an RG tag.
    untagged: int = 0
    first_qname: str | None = None


@dataclass
class _Input:
    path: str
    header: str
    groups: list[dict[str, str]]
    probe: _Probe = field(default_factory=_Probe)

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def platforms(self) -> set[str]:
        """The platforms its read groups declare, else the one its read names
        suggest. An unrecognised PL stands for itself."""
        declared = {
            platform_from_metadata(f"@RG\tID:x\tPL:{g['PL']}") or g["PL"].upper()
            for g in self.groups
            if g.get("PL")
        }
        if declared:
            return declared
        found = platform_from_metadata("", self.probe.first_qname)
        return {found} if found else set()


def _is_cram(path: str) -> bool:
    return path.lower().endswith(".cram")


def _header(samtools: str, path: str) -> str:
    # --no-PG: the header as samtools merge reads it, without view's own @PG.
    try:
        return run_command(
            [samtools, "view", "-H", "--no-PG", path], capture_output=True
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Could not read the header of {path}: {e}") from e


def _sequence_dictionary(header: str) -> list[dict[str, str]]:
    return [
        dict(f.split(":", 1) for f in line.split("\t")[1:] if ":" in f)
        for line in header.splitlines()
        if line.startswith("@SQ\t")
    ]


def _same_reference(headers: Sequence[str]) -> int | None:
    """The first header (by index) whose contigs differ from the earlier ones'
    in name or length, or in MD5 where both list one; None if all agree."""
    names: list[tuple[str | None, str | None]] | None = None
    md5s: list[str | None] = []
    for i, header in enumerate(headers):
        contigs = _sequence_dictionary(header)
        found = [(c.get("SN"), c.get("LN")) for c in contigs]
        if names is None:
            names, md5s = found, [None] * len(found)
        elif found != names:
            return i
        for j, contig in enumerate(contigs):
            md5 = contig.get("M5", "").lower() or None
            if md5 and md5s[j] and md5 != md5s[j]:
                return i
            md5s[j] = md5s[j] or md5
    return None


def _probe(samtools: str, path: str, ref: str | None) -> _Probe:
    cmd = [samtools, "view", "-F", "0x904"]
    if ref and _is_cram(path):
        cmd += ["--reference", ref]
    probe = _Probe()
    proc = popen(
        [*cmd, path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    assert proc.stdout is not None
    stopped = False
    try:
        for line in proc.stdout:
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 11:
                continue
            name = fields[0]
            probe.first_qname = probe.first_qname or name
            flag = int(fields[1])
            seq = fields[9]
            if flag & 16:
                seq = seq[::-1].translate(_COMPLEMENT)
            # As sequenced, so a realigned copy of a run still matches.
            probe.reads[name, flag & 0xC0] = hash(seq)
            tags = fields[11:]
            if any(tag.startswith(("MM:Z:", "Mm:Z:")) for tag in tags):
                probe.modified += 1
            if not any(tag.startswith("RG:Z:") for tag in tags):
                probe.untagged += 1
            if len(probe.reads) >= PROBE_READS:
                stopped = True
                proc.kill()
                break
    finally:
        proc.stdout.close()
        code = proc.wait()
    if code and not stopped:
        raise WGSExtractError(f"Could not read the alignments of {path}.")
    return probe


def _check_shared_reads(inputs: Sequence[_Input]) -> None:
    """Refuse inputs that hold the same reads, or (ONT read IDs naming
    molecules) the same read IDs; warn of other reads that only share a name,
    which duplicate marking and mate pairing take for one template."""
    for i, a in enumerate(inputs):
        for b in inputs[i + 1 :]:
            shared = a.probe.reads.keys() & b.probe.reads.keys()
            same = sum(a.probe.reads[k] == b.probe.reads[k] for k in shared)
            names = {n for n, _ in a.probe.reads} & {n for n, _ in b.probe.reads}
            if same:
                raise WGSExtractError(
                    f"{b.name} repeats {same} of the first {len(a.probe.reads)} "
                    f"reads of {a.name}: the same reads are in both. Merge each "
                    "run once (was a run included twice, or realigned?)."
                )
            if names and a.platforms == b.platforms == {"ont"}:
                raise WGSExtractError(
                    f"{len(names)} read IDs of {a.name} are also in {b.name}. "
                    "ONT read IDs name molecules, so these are the same reads, "
                    "basecalled again; merge one basecall of each run."
                )
            if names:
                warn(
                    "read_name_collision",
                    f"{len(names)} different reads in {a.name} and {b.name} "
                    "have the same names; duplicate marking and mate pairing "
                    "will take them for one template.",
                    inputs=f"{a.name}, {b.name}",
                    reads=len(names),
                )


def _check_modifications(inputs: Sequence[_Input]) -> None:
    with_tags = [x.name for x in inputs if x.probe.modified]
    if with_tags and len(with_tags) < len(inputs):
        without = [x.name for x in inputs if not x.probe.modified]
        warn(
            "mod_tags_partial",
            f"Only {', '.join(with_tags)} have modified-base (MM/ML) tags; "
            f"methylation will come from those runs alone, not "
            f"{', '.join(without)}.",
            inputs=", ".join(without),
        )
    models = {
        m.group(1)
        for x in inputs
        for group in x.groups
        if (m := re.search(r"modbase_models=(\S+)", group.get("DS", "")))
    }
    if len(models) > 1:
        warn(
            "mod_models_differ",
            "The runs' modified bases were called by different models "
            f"({', '.join(sorted(models))}); their methylation calls are pooled.",
            models=", ".join(sorted(models)),
        )


def _sample_name(inputs: Sequence[_Input], sample: str | None, out_file: str) -> str:
    if sample:
        return sample
    named = {x.path: sorted({g["SM"] for g in x.groups if g.get("SM")}) for x in inputs}
    names = {n for found in named.values() for n in found}
    if len(names) > 1:
        listing = "; ".join(f"{f}: {', '.join(n) or 'none'}" for f, n in named.items())
        raise WGSExtractError(
            f"The inputs name different samples ({listing}). Merge only one "
            "person's runs; if they are, name the sample with --sample."
        )
    if names:
        return names.pop()
    stem = Path(out_file).name.rsplit(".", 1)[0]
    warn(
        "sample_name_assumed",
        f"No input names its sample; calling it {stem!r} (set --sample).",
        sample=stem,
    )
    return stem


def _platform(inputs: Sequence[_Input]) -> str | None:
    found = {x.path: x.platforms for x in inputs}
    known = set().union(*found.values())
    if len(known) > 1:
        listing = "; ".join(
            f"{f}: {', '.join(sorted(p)) or 'unknown'}" for f, p in found.items()
        )
        raise WGSExtractError(
            f"The inputs hold reads from different sequencing platforms "
            f"({listing}); callers model one platform's errors, so they are not "
            "merged."
        )
    if not known:
        logging.info("The inputs' sequencing platform is unknown; PL is left unset.")
    platform = known.pop() if known else None
    return platform if platform in PLATFORM_TAGS else None


def _samtools_stem(path: str) -> str:
    """The read-group ID samtools merge -r gives a file's reads."""
    # As samtools does: strip .bam or .sam, then .cram, then the directory.
    if len(path) > 4 and path.endswith((".bam", ".sam")):
        path = path[:-4]
    if len(path) > 5 and path.endswith(".cram"):
        path = path[:-5]
    return path.rsplit("/", 1)[-1]


def _tag_by_file(inputs: Sequence[_Input], attach_rg: bool) -> bool:
    """Whether to name read groups after the files (samtools merge -r): when
    asked, or when an input has none or reads without one. Each input then
    has one read group."""
    untagged = [x for x in inputs if not x.groups or x.probe.untagged]
    if not (attach_rg or untagged):
        return False
    several = [x.name for x in inputs if len(x.groups) > 1]
    if several:
        raise UsageError(
            f"{', '.join(several)} hold several read groups, which naming read "
            "groups after the files would merge into one"
            + (
                "; leave out --attach-rg."
                if not untagged
                else f", and {', '.join(x.name for x in untagged)} "
                "need it for reads without one. Give every read its read group "
                "(e.g. with samtools addreplacerg) before merging."
            )
        )
    stems: dict[str, str] = {}
    for x in inputs:
        stem = _samtools_stem(Path(x.path).as_posix())
        if stem in stems:
            raise UsageError(
                f"{stems[stem]} and {x.path} would both get read group {stem!r}, "
                "named after the file; rename one."
            )
        stems[stem] = x.path
    return True


def _merged_header(
    samtools: str, inputs: Sequence[_Input], flags: list[str], scratch: str
) -> str:
    """The header samtools merge will write: the inputs' headers merged alone
    (as files of the same names, so -r names their read groups alike)."""
    files = []
    for i, x in enumerate(inputs):
        folder = Path(scratch, str(i))
        folder.mkdir()
        stem = _samtools_stem(Path(x.path).as_posix())
        # SAM text, named so that samtools' stripping gives back the stem.
        path = folder / f"{stem}.cram"
        path.write_text(x.header, newline="\n")
        files.append(path.as_posix())
    return run_command(
        [samtools, "merge", "--no-PG", "-O", "SAM", "-o", "-", *flags, *files],
        capture_output=True,
    ).stdout


def _with_sample(header: str, sample: str, platform: str | None) -> str:
    """The header with every read group's SM, and PL where missing, set."""
    lines = []
    for line in header.splitlines():
        if line.startswith("@RG\t"):
            fields = line.split("\t")[1:]
            sm = [i for i, f in enumerate(fields) if f.startswith("SM:")]
            for i in sm:
                fields[i] = f"SM:{sample}"
            if not sm:
                fields.append(f"SM:{sample}")
            if platform and not any(f.startswith("PL:") for f in fields):
                fields.append(f"PL:{PLATFORM_TAGS[platform]}")
            line = "\t".join(["@RG", *fields])
        lines.append(line)
    return "".join(f"{line}\n" for line in lines)


def _check_read_groups(samtools: str, path: str, ref: str | None) -> None:
    """Each of the first reads has an RG tag naming a read group of the header."""
    header = _header(samtools, path)
    ids = {g.get("ID") for g in read_groups(header)}
    cmd = [samtools, "view", *(["--reference", ref] if ref else []), path]
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    assert proc.stdout is not None
    try:
        for count, line in enumerate(proc.stdout):
            m = re.search(r"\tRG:Z:([^\t\n]*)", line)
            if not m or m.group(1) not in ids:
                name = line.split("\t", 1)[0]
                found = (
                    f"read group {m.group(1)!r}, missing from its header"
                    if m
                    else "no read group"
                )
                raise WGSExtractError(f"Read {name} of the merged file has {found}.")
            if count >= PROBE_READS:
                break
    finally:
        proc.kill()
        proc.stdout.close()
        proc.wait()


def _merge_output_path(params: BamMergeParams, outdir: str) -> str:
    ext = ".cram" if params.cram else ".bam"
    name = params.output or f"merged{ext}"
    if not name.lower().endswith((".bam", ".cram")):
        name += ext
    elif name.lower().endswith(".cram") != params.cram:
        raise UsageError(
            f"--output {name} does not match the output format "
            f"({'CRAM' if params.cram else 'BAM'}); add or drop --cram."
        )
    return named_output_path(name, outdir)


def _report_depths(inputs: Sequence[_Input], out_file: str, ref: str | None) -> None:
    from wgsextract_cli.core.genotype_qc import AUTOSOMES, target_depths

    def autosomal(path: str) -> float | None:
        # Sampling scratch goes beside the output: the inputs' folders may be
        # read-only.
        found = target_depths(path, str(Path(out_file).parent), ref)
        depth = found[0].get(AUTOSOMES) if found else None
        return round(depth, 2) if depth is not None else None

    runs = [{"input": x.path, "autosomal_depth": autosomal(x.path)} for x in inputs]
    combined = autosomal(out_file)
    human_output("Autosomal depth:")
    for run in runs:
        depth = run["autosomal_depth"]
        shown = f"{depth:.1f}x" if depth is not None else "not known (no index)"
        human_output(f"  {Path(str(run['input'])).name}: {shown}")
    human_output(
        f"  merged: {combined:.1f}x" if combined is not None else "  merged: not known"
    )
    if combined is not None:
        current_result().add_metrics(autosomal_depth=combined)
    current_result().set_details({"runs": runs})


def run_merge(params: BamMergeParams) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    from wgsextract_cli.core.dependencies import get_tool_path

    samtools = get_tool_path("samtools") or "samtools"
    paths = [fspath(p) for p in params.inputs]
    if len(paths) < 2:
        raise UsageError("bam merge needs at least two input files.")
    verify_paths_exist({f"input {i}": path for i, path in enumerate(paths, 1)})
    for path in paths:
        current_result().add_input(path)
    if len({str(Path(p).resolve()) for p in paths}) != len(paths):
        raise UsageError("The same file is listed more than once.")

    inputs: list[_Input] = []
    for path in paths:
        header = _header(samtools, path)
        if not is_sorted(path, header=header):
            raise WGSExtractError(
                f"{path} is not coordinate-sorted; run 'wgsextract bam sort' first."
            )
        inputs.append(_Input(path, header, read_groups(header)))
    differs = _same_reference([x.header for x in inputs])
    if differs is not None:
        raise WGSExtractError(
            f"{paths[differs]} has a different sequence dictionary (@SQ lines) "
            "than the inputs before it; merge only files aligned to the same "
            "reference."
        )

    threads, _ = get_resource_defaults(params.threads, None)
    outdir = fspath(params.outdir or absolute_path(paths[0]).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    out_file = _merge_output_path(params, outdir)
    if any(Path(out_file).resolve() == Path(p).resolve() for p in paths):
        raise WGSExtractError(f"Output {out_file} would overwrite an input file.")

    ref = None
    if params.cram or any(_is_cram(p) for p in paths):
        ref = resolve_reference(
            fspath(params.ref) if params.ref else None,
            calculate_bam_md5(paths[0], header=inputs[0].header),
            paths[0],
        )
        if not (ref and Path(ref).is_file()):
            raise WGSExtractError(
                LOG_MESSAGES["ref_required_for"].format(task="merging CRAM files")
            )

    for x in inputs:
        x.probe = _probe(samtools, x.path, ref)
    _check_shared_reads(inputs)
    platform = _platform(inputs)
    sample = _sample_name(inputs, params.sample, out_file)
    _check_modifications(inputs)

    # Output is about the size of the inputs; CRAM is roughly half of BAM.
    out_bytes = 0.0
    for path in paths:
        ratio = 1.0 if _is_cram(path) == params.cram else (0.5 if params.cram else 2.0)
        out_bytes += Path(path).stat().st_size * ratio
    needed_gb = int(out_bytes / 1024**3) + 1
    if not check_free_space(outdir, needed_gb):
        raise WGSExtractError(
            f"Insufficient free disk space at {outdir} (needs ~{needed_gb} GB)."
        )

    flags = ["-s", MERGE_SEED]
    by_file = _tag_by_file(inputs, params.attach_rg)
    if by_file:
        flags.append("-r")
        # samtools names the read groups after the text after the last "/".
        paths = [Path(p).as_posix() for p in paths]
    lines: dict[str, set[str]] = {}
    for x in inputs:
        for line in x.header.splitlines():
            if line.startswith("@RG\t") and (m := re.search(r"\tID:([^\t]*)", line)):
                lines.setdefault(m.group(1), set()).add(line)
    conflicting = sorted(i for i, found in lines.items() if len(found) > 1)
    if conflicting and not by_file:
        warn(
            "read_group_conflict",
            f"Read group ID(s) {', '.join(conflicting)} are defined differently in "
            "different inputs; samtools will give them distinct IDs.",
            read_groups=", ".join(conflicting),
        )
    elif not by_file:
        # Identical @RG lines are the same read group split across files.
        flags.append("-c")

    # Name the index explicitly: samtools would otherwise write .csi for BAM,
    # which GATK/htsjdk tools cannot read.
    index = out_file + (".crai" if params.cram else ".bai")
    target = f"{out_file}##idx##{index}"
    out_format = "CRAM" if params.cram else "BAM"
    ref_opts = ["--reference", ref] if ref else []
    logging.info(f"Merging {len(paths)} files into {out_file}...")
    with tempfile.TemporaryDirectory(dir=outdir, prefix=".merge-") as scratch:
        try:
            merged = _merged_header(samtools, inputs, flags, scratch)
            header = _with_sample(merged, sample, platform)
            with cleanup_files(on_failure=[out_file, index]):
                if header == merged:
                    # -f: replace an earlier result like the other commands do.
                    run_command(
                        [samtools, "merge", "-f", "-@", threads, "--write-index"]
                        + ["-O", out_format, "-o", target, *ref_opts, *flags, *paths],
                        capture_output=True,
                    )
                else:
                    header_file = Path(scratch, "header.sam")
                    header_file.write_text(header, newline="\n")
                    run_pipeline(
                        [
                            [samtools, "merge", "-u", "-@", threads, "-o", "-"]
                            + [*ref_opts, *flags, *paths],
                            [samtools, "reheader", "--no-PG", header_file, "-"],
                            [samtools, "view", "-@", threads, "--write-index"]
                            + ["-O", out_format, *ref_opts, "-o", target, "-"],
                        ],
                        description="Merging",
                        stderr_dir=scratch,
                    )
                _check_read_groups(samtools, out_file, ref)
        except (OSError, subprocess.SubprocessError) as e:
            stderr = getattr(e, "stderr", None)
            raise WGSExtractError(
                f"Merging failed: {e}" + (f"\n{stderr.strip()}" if stderr else "")
            ) from e

    current_result().set_reference(ref)
    counts = record_alignment_output(out_file, "merged_alignment")
    groups = read_groups(header)
    current_result().add_metrics(
        merged_inputs=len(paths), read_groups=len(groups), **counts
    )
    logging.info(f"Merged file written to {out_file}")
    logging.info(
        f"Sample {sample}: {len(groups)} read group(s)"
        + (f", platform {PLATFORM_TAGS[platform]}." if platform else ".")
    )
    _report_depths(inputs, out_file, ref)
    if platform not in LONG_READ_PLATFORMS:
        logging.info(
            "If the inputs are lanes of one library, mark duplicates again on the "
            "merged file before variant calling."
        )
