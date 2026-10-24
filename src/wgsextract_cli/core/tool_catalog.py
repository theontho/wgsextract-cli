"""The external tools the CLI uses, and where Pixi provides them."""

import sys

MANDATORY_TOOLS = [
    "samtools",
    "bcftools",
    "tabix",
    "bgzip",
    "bwa",
    "python3",
    "gzip",
    "tar",
]


OPTIONAL_TOOLS = [
    "bwa-mem2",
    "minimap2",
    "pbmm2",
    "pbsv",
    "sniffles",
    "sambamba",
    "samblaster",
    "fastp",
    "fastqc",
    "delly",
    "freebayes",
    "vep",
    "gatk",
    "run_deepvariant",
    "curl",
    "java",
    "yleaf",
    "haplogrep",
    "htsfile",
    "cyrius",
    "run-t1k",
    "ExpansionHunter",
]


def required_dependency_tools(include_python: bool = True) -> list[str]:
    """Return the centrally-defined required dependency tools."""
    if include_python:
        return list(MANDATORY_TOOLS)
    return [tool for tool in MANDATORY_TOOLS if tool != "python3"]


# Optional tools the CLI only uses on some platforms (sys.platform prefixes).
PLATFORM_ONLY_TOOLS = {
    "bwa-mem2": ("linux", "darwin"),
    "sambamba": ("linux",),
    "cyrius": ("linux", "darwin"),
    "run-t1k": ("linux", "darwin"),
    "ExpansionHunter": ("linux", "darwin"),
}


def optional_dependency_tools() -> list[str]:
    """Return the optional dependency tools that apply to this platform."""
    return [
        tool
        for tool in OPTIONAL_TOOLS
        if sys.platform.startswith(PLATFORM_ONLY_TOOLS.get(tool, ("",)))
    ]


PIXI_TOOL_ENVS = {
    "gzip": "default",
    "tar": "default",
    "curl": "default",
    "fastp": "default",
    "fastqc": "default",
    "yleaf": "default",
    "vep": "vep",
    "haplogrep": "default",
    "gatk": "gatk",
    "delly": "default",
    "freebayes": "freebayes",
    "samtools": "default",
    "bcftools": "default",
    "bgzip": "default",
    "tabix": "default",
    "java": "default",
    "sambamba": "default",
    "samblaster": "default",
    "bwa": "default",
    "bwa-mem2": "default",
    "minimap2": "default",
    "pbmm2": "pacbio",
    "pbsv": "pacbio",
    "sniffles": "pacbio",
    "htsfile": "default",
    "cyrius": "cyrius",
    "run-t1k": "t1k",
    "t1k-build.pl": "t1k",
    "ExpansionHunter": "expansionhunter",
    # Only through their own environments (never a `python` on PATH): pixi_env_tool.
    "pharmcat_python": "pharmcat",
    "cyrius_python": "cyrius",
}


# Optional environments the user installs explicitly (`pixi install -e <env>`, as
# documented) and that are never downloaded on first use: Cyrius's license
# allows noncommercial use only, so installing it must be the user's choice.
INSTALL_FIRST_PIXI_ENVS = frozenset({"cyrius", "t1k", "expansionhunter", "pharmcat"})


TOOL_EXECUTABLE_ALIASES = {
    "yleaf": ("yleaf", "Yleaf"),
    "pharmcat_python": ("python",),
    "cyrius_python": ("python",),
}


WINDOWS_PIXI_SCRIPT_LAUNCHERS = {
    "fastqc": ("perl",),
    "gatk": ("python",),
}

# Conda package that provides a tool in its Pixi environment, when the names differ.
PIXI_TOOL_PACKAGES = {
    "gatk": "gatk4",
    "vep": "ensembl-vep",
    "pharmcat_python": "pandas",
    "cyrius_python": "cyrius",
    "run-t1k": "t1k",
    "t1k-build.pl": "t1k",
    "ExpansionHunter": "expansionhunter",
}


def _tool_executable_names(tool: str) -> tuple[str, ...]:
    return TOOL_EXECUTABLE_ALIASES.get(tool, (tool,))
