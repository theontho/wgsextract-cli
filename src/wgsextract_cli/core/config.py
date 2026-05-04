import sys
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

# For Python < 3.11, use tomli
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

APP_NAME = "wgsextract"
APP_AUTHOR = "theontho"

KNOWN_SETTINGS = {
    "input_path": (None, "Default input BAM/CRAM or FASTQ file"),
    "output_directory": (None, "Default output directory"),
    "reference_fasta": (None, "Path to the reference genome FASTA"),
    "reference_library": (None, "Reference library directory (genomes/)"),
    "genome_library": (None, "Directory containing per-person genome folders"),
    "cpu_threads": ("Auto", "Number of CPU threads to use"),
    "memory_limit": ("1G", "Memory limit per thread"),
    "debug_mode": (False, "Enable verbose debug logging"),
    "quiet_mode": (False, "Enable quiet mode (errors only)"),
    "tool_runtime": ("auto", "External tool runtime: auto, native, or wsl"),
    "yleaf_executable": (None, "Path to yleaf executable"),
    "haplogrep_executable": (None, "Path to haplogrep executable"),
    "jar_directory": (None, "Directory for JAR tools"),
    "vep_cache_directory": (None, "Path to VEP cache"),
    "default_input_vcf": (None, "Default input VCF file"),
    "mother_vcf_path": (None, "Default Mother VCF for trios"),
    "father_vcf_path": (None, "Default Father VCF for trios"),
    "batch_file_path": (None, "Path to batch processing CSV/TSV"),
    "vcf_input_paths": (None, "Multiple VCF inputs"),
    "clinvar_vcf_path": (None, "Path to ClinVar VCF data"),
    "revel_tsv_path": (None, "Path to REVEL TSV data"),
    "phylop_tsv_path": (None, "Path to PhyloP TSV data"),
    "gnomad_vcf_path": (None, "Path to gnomAD VCF data"),
    "spliceai_vcf_path": (None, "Path to SpliceAI VCF data"),
    "alphamissense_vcf_path": (None, "Path to AlphaMissense VCF data"),
    "pharmgkb_vcf_path": (None, "Path to PharmGKB data"),
    "pet_r1_fastq": (None, "Test: Path to PET R1 reads"),
    "pet_r2_fastq": (None, "Test: Path to PET R2 reads"),
    "pet_reference_fasta": (None, "Test: Path to PET reference genome"),
}

CONFIG_ALIASES = {
    "input": "input_path",
    "outdir": "output_directory",
    "ref": "reference_fasta",
    "reflib": "reference_library",
    "genomes": "genome_library",
    "threads": "cpu_threads",
    "memory": "memory_limit",
    "runtime": "tool_runtime",
    "yleaf_path": "yleaf_executable",
    "haplogrep_path": "haplogrep_executable",
    "jar_dir": "jar_directory",
    "vep_cache": "vep_cache_directory",
    "input_vcf": "default_input_vcf",
    "mother_vcf": "mother_vcf_path",
    "father_vcf": "father_vcf_path",
}


def get_config_dir() -> Path:
    """Get the standard user configuration directory for this app."""
    return Path(user_config_dir(APP_NAME, APP_AUTHOR))


def get_config_path() -> Path:
    """Get the path to the config.toml file."""
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
        except Exception as e:
            print(f"Error loading config from {config_path}: {e}", file=sys.stderr)

    for old_key, new_key in CONFIG_ALIASES.items():
        if old_key in config and new_key not in config:
            config[new_key] = config[old_key]

    return config


# Global config object
settings = load_config()


def reload_settings() -> None:
    """Re-load configuration from disk and environment."""
    global settings
    new_settings = load_config()
    settings.clear()
    settings.update(new_settings)


def save_config(updates: dict[str, Any]) -> None:
    """Save configuration updates to the config.toml file."""
    import tomli_w

    config_path = get_config_path()

    # Load existing config to merge
    current_config = {}
    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                current_config = tomllib.load(f)
        except Exception:
            pass

    # Update with new values
    for key, value in updates.items():
        if key in KNOWN_SETTINGS:
            if value is None or value == "":
                current_config.pop(key, None)
            else:
                current_config[key] = value

    # Write back
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "wb") as f:
            tomli_w.dump(current_config, f)
        # Refresh global settings
        reload_settings()
    except Exception as e:
        print(f"Error saving config to {config_path}: {e}", file=sys.stderr)
        raise


def get(key: str, default: Any = None) -> Any:
    """Get a configuration value with an optional default."""
    return settings.get(key, default)
