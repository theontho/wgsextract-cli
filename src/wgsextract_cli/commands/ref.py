from wgsextract_cli.core.messages import CLI_HELP

from ._ref_core_commands import (
    cmd_count_ns,
    cmd_download,
    cmd_index,
    cmd_library_list,
    cmd_ref_verify,
)
from ._ref_library_commands import (
    cmd_alphamissense_dl,
    cmd_bootstrap,
    cmd_clinvar_dl,
    cmd_gene_map,
    cmd_gnomad_dl,
    cmd_library,
    cmd_pharmgkb_dl,
    cmd_phylop_dl,
    cmd_revel_dl,
    cmd_spliceai_dl,
)


def register(subparsers, base_parser):
    parser = subparsers.add_parser("ref", help="Reference Data Management commands.")
    ref_subs = parser.add_subparsers(dest="ref_cmd", required=True)

    dl_parser = ref_subs.add_parser(
        "download", parents=[base_parser], help=CLI_HELP["cmd_ref-download"]
    )
    dl_parser.add_argument("--url", required=True, help="URL to download from")
    dl_parser.add_argument("--out", required=True, help="Output FASTA file path")
    dl_parser.set_defaults(func=cmd_download)

    index_parser = ref_subs.add_parser(
        "index", parents=[base_parser], help=CLI_HELP["cmd_ref-index"]
    )
    index_parser.set_defaults(func=cmd_index)

    cntns_parser = ref_subs.add_parser(
        "count-ns", parents=[base_parser], help=CLI_HELP["cmd_ref-count-ns"]
    )
    cntns_parser.set_defaults(func=cmd_count_ns)

    verify_parser = ref_subs.add_parser(
        "verify", parents=[base_parser], help=CLI_HELP["cmd_ref-verify"]
    )
    verify_parser.set_defaults(func=cmd_ref_verify)

    lib_parser = ref_subs.add_parser(
        "library",
        parents=[base_parser],
        help=CLI_HELP["cmd_ref-library"],
    )
    lib_parser.add_argument(
        "--install",
        help="Non-interactively install a genome by its code (e.g., hs38) or index (e.g., 9).",
    )
    lib_parser.add_argument(
        "--list",
        action="store_true",
        help="Non-interactively list all available genomes and their status.",
    )
    lib_parser.set_defaults(func=cmd_library)

    lib_list_parser = ref_subs.add_parser(
        "library-list",
        parents=[base_parser],
        help="List installed and available reference library assets.",
    )
    lib_list_parser.set_defaults(func=cmd_library_list)

    genemap_parser = ref_subs.add_parser(
        "gene-map", parents=[base_parser], help=CLI_HELP["cmd_ref-gene-map"]
    )
    genemap_parser.add_argument(
        "--delete", action="store_true", help="Delete gene maps instead of downloading"
    )
    genemap_parser.set_defaults(func=cmd_gene_map)

    clinvar_dl_parser = ref_subs.add_parser(
        "clinvar",
        parents=[base_parser],
        help="Download official ClinVar VCF for hg19 and hg38.",
    )
    clinvar_dl_parser.set_defaults(func=cmd_clinvar_dl)

    revel_dl_parser = ref_subs.add_parser(
        "revel",
        parents=[base_parser],
        help="Download REVEL pathogenicity scores for hg19 and hg38.",
    )
    revel_dl_parser.set_defaults(func=cmd_revel_dl)

    phylop_dl_parser = ref_subs.add_parser(
        "phylop",
        parents=[base_parser],
        help="Download PhyloP conservation scores for hg19 and hg38.",
    )
    phylop_dl_parser.set_defaults(func=cmd_phylop_dl)

    gnomad_dl_parser = ref_subs.add_parser(
        "gnomad",
        parents=[base_parser],
        help="Download gnomAD sites VCF for hg19 and hg38.",
    )
    gnomad_dl_parser.set_defaults(func=cmd_gnomad_dl)

    spliceai_dl_parser = ref_subs.add_parser(
        "spliceai",
        parents=[base_parser],
        help="Download SpliceAI precomputed scores for hg19 and hg38.",
    )
    spliceai_dl_parser.set_defaults(func=cmd_spliceai_dl)

    alphamissense_dl_parser = ref_subs.add_parser(
        "alphamissense",
        parents=[base_parser],
        help="Download AlphaMissense scores for hg19 and hg38.",
    )
    alphamissense_dl_parser.set_defaults(func=cmd_alphamissense_dl)

    pharmgkb_dl_parser = ref_subs.add_parser(
        "pharmgkb",
        parents=[base_parser],
        help="Download PharmGKB annotations.",
    )
    pharmgkb_dl_parser.set_defaults(func=cmd_pharmgkb_dl)

    bootstrap_parser = ref_subs.add_parser(
        "bootstrap",
        parents=[base_parser],
        help="Download and initialize the reference library bootstrap (VCFs, chains, etc.).",
    )
    bootstrap_parser.add_argument(
        "--install-mappability-maps",
        action="store_true",
        help="Also download the optional mirrored Delly hg19/hg38 mappability maps.",
    )
    bootstrap_parser.set_defaults(func=cmd_bootstrap)
