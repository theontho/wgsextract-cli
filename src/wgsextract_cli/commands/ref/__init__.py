import argparse
from collections.abc import Callable

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api._params import Params
from wgsextract_cli.api.ref import (
    RefAadrParams,
    RefBootstrapParams,
    RefCountNsParams,
    RefDownloadParams,
    RefGeneMapParams,
    RefIndexParams,
    RefLibraryListParams,
    RefLibraryParams,
    RefStatusParams,
    RefVerifyParams,
    RefYbrowseParams,
)
from wgsextract_cli.core.messages import CLI_HELP

from .core_commands import (
    cmd_count_ns,
    cmd_download,
    cmd_index,
    cmd_library_list,
    cmd_ref_verify,
)
from .library_commands import (
    RESOURCE_COMMANDS,
    cmd_aadr,
    cmd_bootstrap,
    cmd_gene_map,
    cmd_library,
    cmd_ybrowse,
)
from .status import cmd_ref_status

Handler = Callable[[argparse.Namespace], None]

# Resource downloads whose help isn't in CLI_HELP.
_RESOURCE_HELP = {
    "clinvar": "Download official ClinVar VCF for hg19 and hg38.",
    "revel": "Download REVEL pathogenicity scores for hg19 and hg38.",
    "phylop": "Download UCSC's PhyloP 100-way conservation scores (~10 GB bigWig).",
    "gnomad": "Download gnomAD population allele frequencies.",
    "spliceai": "Download SpliceAI scores for hg38 (Ensembl, MANE SNVs); index a "
    "manual hg19 download.",
    "alphamissense": "Download AlphaMissense scores for hg19 and hg38.",
    "pharmgkb": "Download PharmGKB annotations.",
    "cadd": CLI_HELP["cmd_ref-cadd"],
    "gerp": CLI_HELP["cmd_ref-gerp"],
}


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser("ref", help="Reference Data Management commands.")
    ref_subs = parser.add_subparsers(dest="ref_cmd", required=True)

    def add(
        name: str, cls: type[Params] | None, func: Handler, help: str
    ) -> argparse.ArgumentParser:
        sub = ref_subs.add_parser(name, parents=[base_parser], help=help)
        if name in ("aadr", "ybrowse"):
            # The CLI asks for these downloads explicitly; the API call is the ask.
            sub.add_argument("--install", action="store_true", required=True)
        if cls is not None:
            add_arguments(sub, cls)
        sub.set_defaults(func=func)
        return sub

    add(
        "aadr",
        RefAadrParams,
        cmd_aadr,
        "Explicitly fetch the hash-pinned CC0 AADR v66.p1 2M SNP panel.",
    )
    add(
        "ybrowse",
        RefYbrowseParams,
        cmd_ybrowse,
        "Explicitly install a hash-pinned, sites-only GRCh38 yBrowse snapshot.",
    )
    add("download", RefDownloadParams, cmd_download, CLI_HELP["cmd_ref-download"])
    add("index", RefIndexParams, cmd_index, CLI_HELP["cmd_ref-index"])
    add("count-ns", RefCountNsParams, cmd_count_ns, CLI_HELP["cmd_ref-count-ns"])
    add("verify", RefVerifyParams, cmd_ref_verify, CLI_HELP["cmd_ref-verify"])
    library = add("library", RefLibraryParams, cmd_library, CLI_HELP["cmd_ref-library"])
    library.add_argument(
        "--list",
        action="store_true",
        help="Non-interactively list all available genomes and their status.",
    )
    add(
        "library-list",
        RefLibraryListParams,
        cmd_library_list,
        "List installed and available reference genomes.",
    )
    status = add(
        "status",
        RefStatusParams,
        cmd_ref_status,
        "Report reference library asset status.",
    )
    status.add_argument(
        "--values",
        action="store_true",
        help="Emit a compact values JSON object for GUI data-source consumers.",
    )
    add("gene-map", RefGeneMapParams, cmd_gene_map, CLI_HELP["cmd_ref-gene-map"])
    for cls, run in RESOURCE_COMMANDS:
        name = cls.COMMAND.removeprefix("ref ")
        add(name, cls, cli_handler(cls, run), _RESOURCE_HELP[name])
    add(
        "bootstrap",
        RefBootstrapParams,
        cmd_bootstrap,
        "Download and initialize the reference library bootstrap (VCFs, chains, etc.).",
    )
