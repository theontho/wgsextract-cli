import difflib
import logging
import os
import sys
from pathlib import Path
from typing import Any, TypeVar, overload

from platformdirs import user_config_dir

from wgsextract_cli.core.atomic_output import atomic_write
from wgsextract_cli.core.paths import expand_path, windows_local_app_dir
from wgsextract_cli.core.utils import WGSExtractError

# For Python < 3.11, use tomli
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

APP_NAME = "wgsextract"
APP_AUTHOR = "theontho"
T = TypeVar("T")
_warned_unknown_keys: set[tuple[str, str]] = set()

KNOWN_SETTINGS = {
    "input_path": (None, "Default input BAM/CRAM or FASTQ file"),
    "output_directory": (None, "Default output directory"),
    "temp_directory": (
        None,
        "Optional volume for sort spills, GATK and realign staging",
    ),
    "atomic_outputs": (
        False,
        "Publish completed main outputs by rename (changes tool provenance paths)",
    ),
    "reference_fasta": (None, "Path to the reference genome FASTA"),
    "reference_library": (None, "Reference library directory (genomes/)"),
    "genome_library": (None, "Directory containing per-person genome folders"),
    "cpu_threads": (
        "auto",
        "CPU threads: a positive integer, or auto (Apple Silicon performance "
        "cores, otherwise all cores)",
    ),
    "memory_limit": (
        "auto",
        "Memory limit per thread, e.g. 2G (auto: 25% of RAM split across threads)",
    ),
    "debug_mode": (False, "Enable verbose debug logging"),
    "quiet_mode": (False, "Enable quiet mode (errors only)"),
    "log_dir": (
        None,
        "Opt-in directory for unique persistent DEBUG run logs and provenance",
    ),
    "keep_awake": (False, "Best-effort sleep inhibition while commands run (opt-in)"),
    "yleaf_executable": (None, "Path to yleaf executable"),
    "haplogrep_executable": (None, "Path to haplogrep executable"),
    "jar_directory": (None, "Directory for JAR tools"),
    "vep_cache_directory": (None, "Path to VEP cache"),
    "pharmcat_directory": (None, "Extracted PharmCAT pipeline release"),
    "parabricks_image": (
        None,
        "NVIDIA Parabricks Docker image for GPU alignment and calling "
        "(default nvcr.io/nvidia/clara/clara-parabricks:4.8.0-1)",
    ),
    "deepvariant_image": (
        None,
        "Google DeepVariant Docker image for CPU calling (default "
        "google/deepvariant:1.10.0)",
    ),
    "deepvariant_ultima_image": (
        None,
        "DeepVariant Docker image with the ULTIMA model, for Ultima reads "
        "(default google/deepvariant:gh1100-ultima)",
    ),
    "default_input_vcf": (None, "Default input VCF file"),
    "microarray_min_gq": (0, "Microarray minimum sample GQ (0 disables filtering)"),
    "microarray_min_dp": (0, "Microarray minimum sample DP (0 disables filtering)"),
    "microarray_assume_hom_ref": (
        True,
        "Treat absent VCF markers on represented contigs as homozygous reference",
    ),
    "microarray_exclude_filtered": (
        False,
        "Write microarray no-calls for VCF FILTER values other than PASS or .",
    ),
    "microarray_call_reasons": (
        False,
        "Write a companion CombinedKit_reasons.tsv with explicit call/no-call reasons",
    ),
    "microarray_strand_ambiguous": (
        "keep",
        "Keep or no-call known biallelic A/T and C/G input SNP sites",
    ),
    "microarray_resolve_rsids": (
        False,
        "Resolve packaged dbSNP rsID aliases and no-call retired/ambiguous IDs",
    ),
    "mother_vcf_path": (None, "Default Mother VCF for trios"),
    "father_vcf_path": (None, "Default Father VCF for trios"),
    "batch_file_path": (None, "Path to batch processing CSV/TSV"),
    "vcf_input_paths": (None, "Multiple VCF inputs"),
    "clinvar_vcf_path": (None, "Path to ClinVar VCF data"),
    "revel_tsv_path": (None, "Path to REVEL TSV data"),
    "phylop_tsv_path": (None, "Path to PhyloP bigWig (or TSV/VCF) data"),
    "cadd_tsv_path": (None, "Path to CADD whole-genome SNV TSV data"),
    "cadd_indel_tsv_path": (None, "Path to CADD indel TSV data"),
    "gerp_bigwig_path": (None, "Path to GERP++ RS bigWig data"),
    "gnomad_vcf_path": (None, "Path to gnomAD VCF data"),
    "spliceai_vcf_path": (None, "Path to SpliceAI VCF data"),
    "alphamissense_vcf_path": (None, "Path to AlphaMissense VCF data"),
    "pharmgkb_vcf_path": (None, "Path to PharmGKB data"),
    "signing_key": (None, "OpenSSH private key used by `sign create`"),
    "pet_r1_fastq": (None, "Test: Path to PET R1 reads"),
    "pet_r2_fastq": (None, "Test: Path to PET R2 reads"),
    "pet_reference_fasta": (None, "Test: Path to PET reference genome"),
    "real_genome_test_path": (None, "Test: Path to a local real genome dataset"),
    "real_genome_tests_enabled": (
        False,
        "Test: Enable real smoke tests from real_genome_test_path",
    ),
    "dev_download_cache": (
        False,
        "Dev: Cache downloads under $XDG_CACHE_HOME/wgsextract for reuse",
    ),
    "dev_download_cache_ttl_seconds": (
        7 * 24 * 60 * 60,
        "Dev: Seconds before an unused dev download cache item is pruned",
    ),
}

PATH_SETTINGS = {
    "log_dir",
    "temp_directory",
    "input_path",
    "output_directory",
    "reference_fasta",
    "reference_library",
    "genome_library",
    "yleaf_executable",
    "haplogrep_executable",
    "jar_directory",
    "vep_cache_directory",
    "pharmcat_directory",
    "default_input_vcf",
    "mother_vcf_path",
    "father_vcf_path",
    "batch_file_path",
    "vcf_input_paths",
    "clinvar_vcf_path",
    "revel_tsv_path",
    "phylop_tsv_path",
    "cadd_tsv_path",
    "cadd_indel_tsv_path",
    "gerp_bigwig_path",
    "gnomad_vcf_path",
    "spliceai_vcf_path",
    "alphamissense_vcf_path",
    "pharmgkb_vcf_path",
    "signing_key",
    "pet_r1_fastq",
    "pet_r2_fastq",
    "pet_reference_fasta",
    "real_genome_test_path",
}


def _normalize_path_setting(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value.expanduser())
    if isinstance(value, str):
        return expand_path(value)
    return value


def normalize_config_paths(config: dict[str, Any]) -> dict[str, Any]:
    for key in PATH_SETTINGS:
        if key in config:
            config[key] = _normalize_path_setting(config[key])
    return config


CONFIG_ALIASES = {
    "input": "input_path",
    "outdir": "output_directory",
    "ref": "reference_fasta",
    "reflib": "reference_library",
    "genomes": "genome_library",
    "threads": "cpu_threads",
    "memory": "memory_limit",
    "yleaf_path": "yleaf_executable",
    "haplogrep_path": "haplogrep_executable",
    "jar_dir": "jar_directory",
    "vep_cache": "vep_cache_directory",
    "input_vcf": "default_input_vcf",
    "mother_vcf": "mother_vcf_path",
    "father_vcf": "father_vcf_path",
}


# Settings of the removed Windows tool runtimes (WSL, MSYS2/pacman, bundled
# Cygwin/MSYS2) that older installers wrote. Every platform now runs its tools
# from the Pixi environment, so these are dropped when the config is read.
LEGACY_SETTINGS = {
    "tool_runtime",
    "runtime",
    "runtime_directory",
    "runtime_dir",
    "pacman_ucrt64_bin",
    "pacman_bin",
}


def _validate_config_types(
    config: dict[str, Any], config_path: Path, *, allow_invalid_threads: bool = False
) -> None:
    for key, value in config.items():
        canonical = CONFIG_ALIASES.get(key, key)
        if canonical not in KNOWN_SETTINGS:
            continue
        default = KNOWN_SETTINGS[canonical][0]
        if canonical == "cpu_threads":
            if allow_invalid_threads:
                continue
            from wgsextract_cli.core.resource_policy import parse_thread_count

            expected = "a positive TOML integer, positive integer string, or 'auto'"
            valid = type(value) is int or isinstance(value, str)
            if valid:
                try:
                    parse_thread_count(value)
                except ValueError:
                    valid = False
        elif canonical == "vcf_input_paths":
            expected = "a TOML string or an array of TOML strings"
            valid = isinstance(value, str) or (
                isinstance(value, list) and all(isinstance(path, str) for path in value)
            )
        elif type(default) is bool:
            expected = "a TOML boolean (true or false)"
            valid = type(value) is bool
        elif type(default) is int:
            expected = "a TOML integer"
            valid = type(value) is int
        else:
            expected = "a TOML string"
            valid = isinstance(value, str)
        if not valid:
            alias = f" ({canonical})" if canonical != key else ""
            raise WGSExtractError(
                f"Invalid setting {key!r}{alias} in {config_path}: "
                f"expected {expected}; got {type(value).__name__}."
            )


def get_config_dir() -> Path:
    """Get the standard user configuration directory for this app."""
    return windows_local_app_dir(APP_NAME, APP_AUTHOR) or Path(
        user_config_dir(APP_NAME, APP_AUTHOR)
    )


# Set from --config: a caller (such as the GUI) keeping its own settings file.
_config_path_override: Path | None = None


def set_config_path(path: str | os.PathLike[str] | None) -> None:
    """Use this config.toml instead of the per-user one (None restores it)."""
    global _config_path_override
    _config_path_override = Path(path).expanduser().resolve() if path else None


def config_args(*, keep_awake: bool | None = None) -> list[str]:
    """Forward the config file and sleep override without changing default argv."""
    args = ["--config", str(_config_path_override)] if _config_path_override else []
    if keep_awake or (keep_awake is False and settings.get("keep_awake", False)):
        args.append("--keep-awake" if keep_awake else "--no-keep-awake")
    from wgsextract_cli.core.temp_directory import configured_temp_directory

    temporary = configured_temp_directory()
    if temporary:
        args += ["--tmp-dir", temporary]
    return args


def get_config_path() -> Path:
    """Get the path to the config.toml file."""
    if _config_path_override is not None:
        return _config_path_override
    # Priority for macOS: Use ~/.config/wgsextract/ if ~/.config exists
    if sys.platform == "darwin":
        dot_config = Path.home() / ".config"
        if dot_config.is_dir():
            return dot_config / APP_NAME / "config.toml"

    return get_config_dir() / "config.toml"


def load_config() -> dict[str, Any]:
    """
    Load configuration from the standard config file.
    """
    config = {}

    # Load from config.toml
    config_path = get_config_path()
    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                config = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            logging.warning(f"Ignoring unreadable config {config_path}: {e}")

    for legacy_key in LEGACY_SETTINGS & config.keys():
        logging.debug(f"Ignoring obsolete setting {legacy_key!r} in {config_path}")
        del config[legacy_key]

    for old_key, new_key in CONFIG_ALIASES.items():
        if old_key in config and new_key not in config:
            config[new_key] = config[old_key]

    for key in config.keys() - KNOWN_SETTINGS.keys() - CONFIG_ALIASES.keys():
        identity = (str(config_path), key)
        if identity in _warned_unknown_keys:
            continue
        _warned_unknown_keys.add(identity)
        matches = difflib.get_close_matches(key, KNOWN_SETTINGS, n=1)
        hint = f" Did you mean {matches[0]!r}?" if matches else ""
        logging.warning(f"Unknown setting {key!r} in {config_path}.{hint}")

    _validate_config_types(config, config_path, allow_invalid_threads=True)
    return normalize_config_paths(config)


# main selects --config before loading; importers share this stable dictionary.
settings: dict[str, Any] = {}


def reload_settings() -> None:
    """Re-load configuration from disk and drop results derived from it."""
    from wgsextract_cli.core.dependencies import clear_tool_caches

    new_settings = load_config()
    settings.clear()
    settings.update(new_settings)
    clear_tool_caches()


def save_config(updates: dict[str, Any]) -> None:
    """Save configuration updates to the config.toml file."""
    import tomli_w

    config_path = get_config_path()

    current_config = {}
    if config_path.exists():
        # Refuse to overwrite a config we cannot parse; that would drop the
        # user's other settings.
        try:
            with open(config_path, "rb") as f:
                current_config = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise WGSExtractError(
                f"Cannot update {config_path}: it is not valid TOML ({e})."
            ) from e

    for legacy_key in LEGACY_SETTINGS:
        current_config.pop(legacy_key, None)
    for key, value in updates.items():
        if key not in KNOWN_SETTINGS:
            continue
        # Aliases are mapped onto the canonical key on load, so a stale alias
        # would resurrect a cleared value or shadow nothing but confuse users.
        for alias, canonical in CONFIG_ALIASES.items():
            if canonical == key:
                current_config.pop(alias, None)
        if value is None or value == "":
            current_config.pop(key, None)
        else:
            current_config[key] = _normalize_path_setting(value)

    _validate_config_types(current_config, config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(config_path, tomli_w.dumps(current_config).encode("utf-8"))
    reload_settings()


@overload
def get(key: str) -> object | None: ...


@overload
def get(key: str, default: T) -> object | T: ...


def get(key: str, default: object | None = None) -> object | None:
    """Get a configuration value with an optional default."""
    return settings.get(key, default)
