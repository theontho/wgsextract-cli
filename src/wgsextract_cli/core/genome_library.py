import logging
import re
import sys
from argparse import Namespace
from pathlib import Path
from typing import Any

import tomli_w

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.utils import WGSExtractError

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ALIGNMENT_SUFFIXES = (".cram", ".bam")
VCF_SUFFIXES = (".vcf.gz", ".vcf.bgz", ".vcf")
FASTQ_SUFFIXES = (".fastq.gz", ".fq.gz", ".fastq", ".fq")
GENOME_CONFIG_NAME = "genome-config.toml"
GENOME_CONFIG_KEYS = ("alignment", "vcf", "fastq_r1", "fastq_r2")
_FASTQ_EXT_RE = re.compile(r"\.(?:fastq|fq)(?:\.gz)?$", re.IGNORECASE)
# Mate markers are whole name tokens (split on . _ -): an explicit R1/read2 token
# anywhere (sample_R1_val_1, sample_R2.trimmed), else a trailing bare 1/2,
# optionally followed by an Illumina segment number (sample_1, sample.2_001).
_EXPLICIT_MATE_RE = re.compile(r"(?<![^._-])(?:r|read)([12])(?![^._-])", re.IGNORECASE)
_TRAILING_MATE_RE = re.compile(r"(?<![^._-])([12])((?:[._-]\d{3})?)$")
VCF_FILE_COMMANDS = {
    "annotate",
    "filter",
    "clinvar",
    "revel",
    "phylop",
    "cadd",
    "gerp",
    "gnomad",
    "spliceai",
    "alphamissense",
    "pharmgkb",
    "chain-annotate",
}


def apply_genome_selection(args: Namespace, explicit_dests: set[str]) -> None:
    """Resolve --genome to configured or discovered inputs and per-genome outputs."""
    genome_id = getattr(args, "genome", None)
    if not genome_id:
        return

    root = settings.get("genome_library")
    if not root:
        raise WGSExtractError(
            "--genome requires genome_library to be set in config.toml."
        )

    root_dir = Path(root).expanduser()
    genome_dir = root_dir / str(genome_id)
    if not genome_dir.resolve().is_relative_to(root_dir.resolve()):
        raise WGSExtractError(f"Genome ID cannot escape genome_library: {genome_id}")
    if not genome_dir.is_dir():
        raise WGSExtractError(
            f"Genome '{genome_id}' not found in genome_library: {genome_dir}"
        )

    config = _load_or_create_genome_config(genome_dir)
    args.genome_dir = str(genome_dir)
    if "outdir" not in explicit_dests and hasattr(args, "outdir"):
        args.outdir = str(genome_dir)

    command = getattr(args, "command", None)
    if command == "vcf" or getattr(args, "qc_cmd", None) == "vcf":
        _set_vcf_input(args, genome_dir, config, explicit_dests)
    elif command in {"align", "pet-align"} or getattr(args, "qc_cmd", None) == "fastp":
        _set_fastq_inputs(args, genome_dir, config, explicit_dests)
    else:
        _set_alignment_input(args, genome_dir, config, explicit_dests)

    if getattr(args, "input", None):
        logging.debug(f"Resolved --genome {genome_id} input: {args.input}")
    if getattr(args, "vcf_input", None):
        logging.debug(f"Resolved --genome {genome_id} VCF input: {args.vcf_input}")


def _load_or_create_genome_config(genome_dir: Path) -> dict[str, Any]:
    config_path = genome_dir / GENOME_CONFIG_NAME
    raw = _load_genome_config(config_path)
    existing = {key: value for key, value in raw.items() if isinstance(value, str)}
    discovered = _discover_genome_files(genome_dir, existing)
    additions = {
        key: value for key, value in discovered.items() if value and key not in raw
    }

    if additions or not config_path.exists():
        _write_genome_config(config_path, {**raw, **additions})

    return {**existing, **additions}


def _load_genome_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise WGSExtractError(f"Failed to read {config_path}: {e}") from e


def _discover_genome_files(genome_dir: Path, config: dict[str, Any]) -> dict[str, str]:
    alignments = _alignment_candidates(genome_dir, config)
    vcfs = _find_files(genome_dir, VCF_SUFFIXES)
    fastq_sets = _find_fastq_sets(genome_dir)
    pacbio_reads = [
        path
        for path in _find_files(genome_dir, (".bam",))
        if _is_pacbio_reads_bam(path)
    ]

    discovered: dict[str, str] = {}
    if len(alignments) == 1:
        discovered["alignment"] = _relative(alignments[0], genome_dir)
    if len(vcfs) == 1:
        discovered["vcf"] = _relative(vcfs[0], genome_dir)
    if len(fastq_sets) == 1:
        r1, r2 = fastq_sets[0]
        discovered["fastq_r1"] = _relative(r1, genome_dir)
        if r2:
            discovered["fastq_r2"] = _relative(r2, genome_dir)
    elif len(pacbio_reads) == 1:
        discovered["fastq_r1"] = _relative(pacbio_reads[0], genome_dir)
    return discovered


def _write_genome_config(config_path: Path, config: dict[str, Any]) -> None:
    """Write the known path keys (or commented placeholders), keeping any other keys."""
    lines = [
        "# WGS Extract per-genome configuration",
        "# Paths are relative to this genome folder unless absolute.",
        "# Edit these values to resolve ambiguity when multiple files exist.",
        "",
    ]
    for key in GENOME_CONFIG_KEYS:
        if key in config:
            lines.append(tomli_w.dumps({key: config[key]}).rstrip("\n"))
        else:
            lines.append(f'# {key} = "relative/path/to/file"')
    extra = {k: v for k, v in config.items() if k not in GENOME_CONFIG_KEYS}
    text = "\n".join(lines) + "\n"
    if extra:
        text += "\n" + tomli_w.dumps(extra)
    config_path.write_text(text)


def _set_alignment_input(
    args: Namespace,
    genome_dir: Path,
    config: dict[str, Any],
    explicit_dests: set[str],
) -> None:
    if "input" in explicit_dests or not hasattr(args, "input"):
        return
    args.input = str(_resolve_alignment(genome_dir, config))


def _set_vcf_input(
    args: Namespace,
    genome_dir: Path,
    config: dict[str, Any],
    explicit_dests: set[str],
) -> None:
    vcf_cmd = getattr(args, "vcf_cmd", None)
    if vcf_cmd == "trio":
        if "proband" in explicit_dests and getattr(args, "proband", None):
            return
        if "vcf_input" in explicit_dests and getattr(args, "vcf_input", None):
            return
        resolved_vcf = str(_resolve_category(genome_dir, config, "vcf", VCF_SUFFIXES))
        if hasattr(args, "proband"):
            args.proband = resolved_vcf
        if "vcf_input" not in explicit_dests and hasattr(args, "vcf_input"):
            args.vcf_input = resolved_vcf
        if "input" not in explicit_dests and hasattr(args, "input"):
            args.input = None
        return
    if "vcf_input" in explicit_dests and getattr(args, "vcf_input", None):
        return
    should_resolve_vcf = getattr(args, "qc_cmd", None) == "vcf" or (
        vcf_cmd in VCF_FILE_COMMANDS and "vcf_input" not in explicit_dests
    )
    if should_resolve_vcf and hasattr(args, "vcf_input"):
        args.vcf_input = str(_resolve_category(genome_dir, config, "vcf", VCF_SUFFIXES))
        if "input" not in explicit_dests and hasattr(args, "input"):
            args.input = None
        return
    _set_alignment_input(args, genome_dir, config, explicit_dests)


def _set_fastq_inputs(
    args: Namespace,
    genome_dir: Path,
    config: dict[str, Any],
    explicit_dests: set[str],
) -> None:
    if "r1" in explicit_dests and "r2" in explicit_dests:
        return

    if "r1" in explicit_dests and getattr(args, "r1", None):
        if _is_pacbio_reads_bam(Path(args.r1).expanduser()):
            return
        r2 = _matching_r2(
            Path(args.r1).expanduser().resolve(),
            _find_files(genome_dir, FASTQ_SUFFIXES),
        )
        if r2 and "r2" not in explicit_dests and hasattr(args, "r2"):
            args.r2 = str(r2)
        return

    if "r2" in explicit_dests and getattr(args, "r2", None):
        r1 = _matching_r1(
            Path(args.r2).expanduser().resolve(),
            _find_files(genome_dir, FASTQ_SUFFIXES),
        )
        if r1 and "r1" not in explicit_dests and hasattr(args, "r1"):
            args.r1 = str(r1)
        return

    r1_config = config.get("fastq_r1")
    r2_config = config.get("fastq_r2")
    if r1_config:
        if "r1" not in explicit_dests and hasattr(args, "r1"):
            args.r1 = str(_resolve_config_path(genome_dir, r1_config, "fastq_r1"))
        if r2_config and "r2" not in explicit_dests and hasattr(args, "r2"):
            args.r2 = str(_resolve_config_path(genome_dir, r2_config, "fastq_r2"))
        return

    fastq_sets = _find_fastq_sets(genome_dir)
    if not fastq_sets:
        pacbio_reads = [
            path
            for path in _find_files(genome_dir, (".bam",))
            if _is_pacbio_reads_bam(path)
        ]
        if len(pacbio_reads) == 1:
            if "r1" not in explicit_dests and hasattr(args, "r1"):
                args.r1 = str(pacbio_reads[0])
            return
        if len(pacbio_reads) > 1:
            raise _ambiguous_error(
                genome_dir, "PacBio read BAM", "fastq_r1", pacbio_reads
            )
        return
    if len(fastq_sets) > 1:
        raise _ambiguous_error(
            genome_dir, "FASTQ set", "fastq_r1 and fastq_r2", fastq_sets
        )

    r1, r2 = fastq_sets[0]
    if "r1" not in explicit_dests and hasattr(args, "r1"):
        args.r1 = str(r1)
    if r2 and "r2" not in explicit_dests and hasattr(args, "r2"):
        args.r2 = str(r2)


def _resolve_category(
    genome_dir: Path,
    config: dict[str, Any],
    config_key: str,
    suffixes: tuple[str, ...],
) -> Path:
    configured = config.get(config_key)
    if configured:
        return _resolve_config_path(genome_dir, configured, config_key)

    candidates = _find_files(genome_dir, suffixes)
    if not candidates:
        raise WGSExtractError(
            f"No {config_key} file found under {genome_dir}. Add {config_key} to {genome_dir / GENOME_CONFIG_NAME}."
        )
    if len(candidates) > 1:
        raise _ambiguous_error(genome_dir, config_key, config_key, candidates)
    return candidates[0]


def _resolve_alignment(genome_dir: Path, config: dict[str, Any]) -> Path:
    configured = config.get("alignment")
    if configured:
        return _resolve_config_path(genome_dir, configured, "alignment")

    candidates = _alignment_candidates(genome_dir, config)
    if not candidates:
        raise WGSExtractError(
            f"No alignment file found under {genome_dir}. Add alignment to {genome_dir / GENOME_CONFIG_NAME}."
        )
    if len(candidates) > 1:
        raise _ambiguous_error(genome_dir, "alignment", "alignment", candidates)
    return candidates[0]


def _alignment_candidates(genome_dir: Path, config: dict[str, Any]) -> list[Path]:
    fastq_r1 = config.get("fastq_r1")
    fastq_path = None
    if fastq_r1:
        try:
            fastq_path = _resolve_config_path(genome_dir, fastq_r1, "fastq_r1")
        except WGSExtractError:
            fastq_path = None
    return [
        path
        for path in _find_files(genome_dir, ALIGNMENT_SUFFIXES)
        if path != fastq_path and not _is_pacbio_reads_bam(path)
    ]


def _resolve_config_path(genome_dir: Path, configured: str, config_key: str) -> Path:
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = genome_dir / path
    if not path.is_file():
        raise WGSExtractError(
            f"Configured {config_key} does not exist: {path}. Edit {genome_dir / GENOME_CONFIG_NAME}."
        )
    return path


def _find_files(folder: Path, suffixes: tuple[str, ...]) -> list[Path]:
    candidates = [
        p for p in folder.rglob("*") if _is_data_file(p) and _has_suffix(p, suffixes)
    ]
    candidates.sort(
        key=lambda p: (_suffix_rank(p, suffixes), str(p.relative_to(folder)).lower())
    )
    return candidates


def _find_fastq_sets(folder: Path) -> list[tuple[Path, Path | None]]:
    fastqs = _find_files(folder, FASTQ_SUFFIXES)
    r1s = [p for p in fastqs if _fastq_rank(p) == 0]
    r2s = [p for p in fastqs if _fastq_rank(p) == 1]
    singles = [p for p in fastqs if _fastq_rank(p) == 2]

    if r1s:
        sets: list[tuple[Path, Path | None]] = []
        used_r2: set[Path] = set()
        for r1 in r1s:
            r2 = _matching_r2(r1, r2s)
            if r2:
                used_r2.add(r2)
            sets.append((r1, r2))
        sets.extend((r2, None) for r2 in r2s if r2 not in used_r2)
        sets.extend((single, None) for single in singles)
        return sets
    return [(single, None) for single in singles]


def _is_pacbio_reads_bam(path: Path) -> bool:
    return path.name.lower().endswith((".ccs.bam", ".subreads.bam"))


def _matching_r2(r1: Path, r2s: list[Path]) -> Path | None:
    stem = _fastq_pair_key(r1)
    for r2 in r2s:
        if _fastq_rank(r2) == 1 and _fastq_pair_key(r2) == stem:
            return r2
    return None


def _matching_r1(r2: Path, fastqs: list[Path]) -> Path | None:
    stem = _fastq_pair_key(r2)
    for r1 in fastqs:
        if _fastq_rank(r1) == 0 and _fastq_pair_key(r1) == stem:
            return r1
    return None


def pair_stem(stem: str) -> str:
    """A read file's stem without its mate marker (HG00096_R1 -> HG00096), for
    naming what both mates produce together."""
    explicit = list(_EXPLICIT_MATE_RE.finditer(stem))
    match = explicit[-1] if explicit else _TRAILING_MATE_RE.search(stem)
    if not match:
        return stem
    start, end = match.span()
    # A trailing match also covers the Illumina segment (_001), which stays.
    kept = "" if explicit else match.group(2)
    if start > 0 and stem[start - 1] in "._-":
        start -= 1
    elif end < len(stem) and stem[end] in "._-":
        end += 1
    return stem[:start] + kept + stem[end:] or stem


def _fastq_pair_key(path: Path) -> str:
    """Name with every mate marker masked, so R1 and R2 of one sample share a key."""
    stem = _FASTQ_EXT_RE.sub("", path.name.lower())
    stem = _EXPLICIT_MATE_RE.sub(lambda m: m.group(0)[:-1] + "#", stem)
    stem = _TRAILING_MATE_RE.sub(lambda m: "#" + m.group(2), stem)
    return str(path.parent.resolve() / stem)


def _is_data_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.name != GENOME_CONFIG_NAME
        and not path.name.startswith(".")
    )


def _has_suffix(path: Path, suffixes: tuple[str, ...]) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in suffixes)


def _suffix_rank(path: Path, suffixes: tuple[str, ...]) -> int:
    name = path.name.lower()
    for index, suffix in enumerate(suffixes):
        if name.endswith(suffix):
            return index
    return len(suffixes)


def _fastq_rank(path: Path) -> int:
    """0 for read 1, 1 for read 2, 2 for unpaired/unknown."""
    stem = _FASTQ_EXT_RE.sub("", path.name)
    explicit = list(_EXPLICIT_MATE_RE.finditer(stem))
    match = explicit[-1] if explicit else _TRAILING_MATE_RE.search(stem)
    return int(match.group(1)) - 1 if match else 2


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _ambiguous_error(
    genome_dir: Path,
    label: str,
    config_key: str,
    candidates: list[Path] | list[tuple[Path, Path | None]],
) -> WGSExtractError:
    config_path = genome_dir / GENOME_CONFIG_NAME
    rendered = []
    for candidate in candidates:
        if isinstance(candidate, tuple):
            parts = [_relative(path, genome_dir) for path in candidate if path]
            rendered.append(" + ".join(parts))
        else:
            rendered.append(_relative(candidate, genome_dir))
    return WGSExtractError(
        f"Ambiguous {label} files under {genome_dir}: {', '.join(rendered)}. "
        f"Edit {config_path} and set {config_key} to the intended relative path."
    )
