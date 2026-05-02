import os
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
    "cpu_threads": ("Auto", "Number of CPU threads to use"),
    "memory_limit": ("1G", "Memory limit per thread"),
    "debug_mode": (False, "Enable verbose debug logging"),
    "quiet_mode": (False, "Enable quiet mode (errors only)"),
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
    "skip_dotenv": (False, "Skip loading .env files (Internal/Test use)"),
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
    Also handles merging with environment variables.
    """
    config = {}

    # 1. Load from config.toml
    config_path = get_config_path()
    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                config = tomllib.load(f)
        except Exception as e:
            print(f"Error loading config from {config_path}: {e}", file=sys.stderr)

    # 2. Merge with environment variables (env vars take precedence)
    # Mapping of ENV_VAR to config key
    env_mapping = {
        "WGSE_INPUT_PATH": "input_path",
        "WGSE_OUTPUT_DIRECTORY": "output_directory",
        "WGSE_REFERENCE_FASTA": "reference_fasta",
        "WGSE_REFERENCE_LIBRARY": "reference_library",
        "WGSE_CPU_THREADS": "cpu_threads",
        "WGSE_MEMORY_LIMIT": "memory_limit",
        "WGSE_DEBUG_MODE": "debug_mode",
        "WGSE_QUIET_MODE": "quiet_mode",
        "WGSE_YLEAF_EXECUTABLE": "yleaf_executable",
        "WGSE_HAPLOGREP_EXECUTABLE": "haplogrep_executable",
        "WGSE_VEP_CACHE_DIRECTORY": "vep_cache_directory",
        "WGSE_BATCH_FILE_PATH": "batch_file_path",
        "WGSE_VCF_INPUT_PATHS": "vcf_input_paths",
        "WGSE_DEFAULT_INPUT_VCF": "default_input_vcf",
        "WGSE_MOTHER_VCF_PATH": "mother_vcf_path",
        "WGSE_FATHER_VCF_PATH": "father_vcf_path",
        "WGSE_JAR_DIRECTORY": "jar_directory",
        "WGSE_CLINVAR_VCF_PATH": "clinvar_vcf_path",
        "WGSE_REVEL_TSV_PATH": "revel_tsv_path",
        "WGSE_PHYLOP_TSV_PATH": "phylop_tsv_path",
        "WGSE_GNOMAD_VCF_PATH": "gnomad_vcf_path",
        "WGSE_SPLICEAI_VCF_PATH": "spliceai_vcf_path",
        "WGSE_ALPHAMISSENSE_VCF_PATH": "alphamissense_vcf_path",
        "WGSE_PHARMGKB_VCF_PATH": "pharmgkb_vcf_path",
        "WGSE_PET_R1_FASTQ": "pet_r1_fastq",
        "WGSE_PET_R2_FASTQ": "pet_r2_fastq",
        "WGSE_PET_REFERENCE_FASTA": "pet_reference_fasta",
        "WGSE_SKIP_DOTENV": "skip_dotenv",
    }

    for env_var, config_key in env_mapping.items():
        val = os.environ.get(env_var)
        if val is not None:
            # Type conversion for common types
            if config_key == "cpu_threads" and val:
                try:
                    config[config_key] = int(val)
                except ValueError:
                    pass
            elif config_key in ("debug_mode", "quiet_mode", "skip_dotenv"):
                config[config_key] = val == "1" or val.lower() == "true"
            else:
                config[config_key] = val

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
