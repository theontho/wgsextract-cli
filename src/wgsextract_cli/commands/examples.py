import logging
import shutil
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.genome_library import GENOME_CONFIG_NAME
from wgsextract_cli.core.utils import WGSExtractError, run_command

FTP_ROOT = "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp"
ASPERA_ROOT = "fasp-g1k@fasp.1000genomes.ebi.ac.uk:/vol1/ftp"
COLLECTION_DIR = "test-1000genomes"


@dataclass(frozen=True)
class ExampleFile:
    url_path: str
    role: str


@dataclass(frozen=True)
class GenomeExample:
    example_id: str
    sample: str
    label: str
    data_type: str
    size: str
    description: str
    files: tuple[ExampleFile, ...]


EXAMPLES = (
    GenomeExample(
        example_id="na12878-lowcov-fastq",
        sample="NA12878",
        label="NA12878 low-coverage paired FASTQ",
        data_type="fastq",
        size="low coverage",
        description="Phase 3 low-coverage Illumina WGS paired reads.",
        files=(
            ExampleFile(
                "phase3/data/NA12878/sequence_read/NA12878.illumina.wgs.low_coverage.20101123.read1.fastq.gz",
                "fastq_r1",
            ),
            ExampleFile(
                "phase3/data/NA12878/sequence_read/NA12878.illumina.wgs.low_coverage.20101123.read2.fastq.gz",
                "fastq_r2",
            ),
        ),
    ),
    GenomeExample(
        example_id="na12878-lowcov-bam",
        sample="NA12878",
        label="NA12878 low-coverage BAM",
        data_type="bam",
        size="low coverage",
        description="Phase 3 low-coverage GRCh37 alignment.",
        files=(
            ExampleFile(
                "phase3/data/NA12878/alignment/NA12878.mapped.ILLUMINA.bwa.CEU.low_coverage.20120522.bam",
                "alignment",
            ),
            ExampleFile(
                "phase3/data/NA12878/alignment/NA12878.mapped.ILLUMINA.bwa.CEU.low_coverage.20120522.bam.bai",
                "alignment_index",
            ),
        ),
    ),
    GenomeExample(
        example_id="na12878-exome-bam",
        sample="NA12878",
        label="NA12878 exome BAM",
        data_type="bam",
        size="exome",
        description="Phase 3 exome alignment for faster alignment-file workflows.",
        files=(
            ExampleFile(
                "phase3/data/NA12878/alignment/NA12878.mapped.ILLUMINA.bwa.CEU.exome.20121211.bam",
                "alignment",
            ),
            ExampleFile(
                "phase3/data/NA12878/alignment/NA12878.mapped.ILLUMINA.bwa.CEU.exome.20121211.bam.bai",
                "alignment_index",
            ),
        ),
    ),
    GenomeExample(
        example_id="na12878-30x-cram",
        sample="NA12878",
        label="NA12878 30x CRAM",
        data_type="cram",
        size="30x whole genome",
        description="High-coverage GRCh38 CRAM for full-size workflow testing.",
        files=(
            ExampleFile(
                "data_collections/1000_genomes_project/30x_grch38/data/NA12878/NA12878.final.cram",
                "alignment",
            ),
            ExampleFile(
                "data_collections/1000_genomes_project/30x_grch38/data/NA12878/NA12878.final.cram.crai",
                "alignment_index",
            ),
        ),
    ),
    GenomeExample(
        example_id="phase3-chrmt-vcf",
        sample="phase3",
        label="Phase 3 chrMT VCF",
        data_type="vcf",
        size="small chromosome",
        description="Small mitochondrial VCF for quick variant workflow smoke tests.",
        files=(
            ExampleFile(
                "release/20130502/ALL.chrMT.phase3_callmom-v0_4.20130502.genotypes.vcf.gz",
                "vcf",
            ),
            ExampleFile(
                "release/20130502/ALL.chrMT.phase3_callmom-v0_4.20130502.genotypes.vcf.gz.tbi",
                "vcf_index",
            ),
        ),
    ),
    GenomeExample(
        example_id="phase3-chr20-vcf",
        sample="phase3",
        label="Phase 3 chr20 VCF",
        data_type="vcf",
        size="single chromosome",
        description="Integrated Phase 3 chr20 genotypes for moderate VCF workflows.",
        files=(
            ExampleFile(
                "release/20130502/ALL.chr20.phase3_shapeit2_mvncall_integrated_v5.20130502.genotypes.vcf.gz",
                "vcf",
            ),
            ExampleFile(
                "release/20130502/ALL.chr20.phase3_shapeit2_mvncall_integrated_v5.20130502.genotypes.vcf.gz.tbi",
                "vcf_index",
            ),
        ),
    ),
)

EXAMPLES_BY_ID = {example.example_id: example for example in EXAMPLES}
CONFIG_ROLES = {"alignment", "vcf", "fastq_r1", "fastq_r2"}


def register(subparsers, base_parser):
    parser = subparsers.add_parser(
        "examples",
        parents=[base_parser],
        help="Download curated 1000 Genomes example datasets.",
    )
    examples_subs = parser.add_subparsers(dest="examples_cmd", required=True)

    list_parser = examples_subs.add_parser(
        "list",
        parents=[base_parser],
        help="List curated 1000 Genomes examples.",
    )
    list_parser.add_argument(
        "--target-root",
        help=(
            "Genome library root. Defaults to config genome_library, or repo-root "
            "genomes/ when unset."
        ),
    )
    list_parser.set_defaults(func=cmd_list)

    download_parser = examples_subs.add_parser(
        "download",
        parents=[base_parser],
        help="Download curated 1000 Genomes examples into the genome library.",
    )
    download_parser.add_argument(
        "example_ids",
        nargs="*",
        metavar="EXAMPLE_ID",
        help="Example IDs to download. Defaults to a small starter set.",
    )
    download_parser.add_argument(
        "--all",
        action="store_true",
        help="Download every curated example, including large full-genome data.",
    )
    download_parser.add_argument(
        "--method",
        choices=("auto", "ftp", "aspera"),
        default="auto",
        help="Transfer method. Auto uses FTP unless Aspera is explicitly requested.",
    )
    download_parser.add_argument(
        "--aspera-key",
        help="Private key for Aspera ascp downloads. Required with --method aspera.",
    )
    download_parser.add_argument(
        "--target-root",
        help=(
            "Genome library root. Defaults to config genome_library, or repo-root "
            "genomes/ when unset."
        ),
    )
    download_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download files that already exist.",
    )
    download_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned downloads without creating files.",
    )
    download_parser.set_defaults(func=cmd_download)


def cmd_list(args: Namespace) -> None:
    root = _target_root(args.target_root)
    print(f"Target collection: {root / COLLECTION_DIR}")
    print()
    for example in EXAMPLES:
        print(f"{example.example_id}")
        print(f"  label: {example.label}")
        print(f"  type:  {example.data_type}")
        print(f"  size:  {example.size}")
        print(f"  files: {len(example.files)}")
        print(f"  use:   wgsextract --genome {COLLECTION_DIR}/{example.example_id} ...")
        print()


def cmd_download(args: Namespace) -> None:
    selected = _select_examples(args.example_ids, args.all)
    aspera_key = args.aspera_key
    method = _resolve_method(args.method, aspera_key)
    root = _target_root(args.target_root)
    collection_dir = root / COLLECTION_DIR

    logging.info("Downloading examples into %s", collection_dir)
    for example in selected:
        example_dir = collection_dir / example.example_id
        planned = _planned_downloads(example, example_dir, method)
        if args.dry_run:
            _print_plan(example, planned)
            continue

        example_dir.mkdir(parents=True, exist_ok=True)
        for source, destination, _role in planned:
            if destination.exists() and not args.force:
                logging.info("Skipping existing %s", destination)
                continue
            _download_file(source, destination, method, aspera_key)
        _write_genome_config(example, example_dir)
        logging.info(
            "Installed %s. Use --genome %s/%s",
            example.label,
            COLLECTION_DIR,
            example.example_id,
        )


def _select_examples(example_ids: list[str], include_all: bool) -> list[GenomeExample]:
    if include_all:
        if example_ids:
            raise WGSExtractError("Use either --all or explicit example IDs, not both.")
        return list(EXAMPLES)
    if not example_ids:
        return [
            EXAMPLES_BY_ID["phase3-chrmt-vcf"],
            EXAMPLES_BY_ID["na12878-lowcov-bam"],
        ]

    selected = []
    unknown = []
    for example_id in example_ids:
        example = EXAMPLES_BY_ID.get(example_id)
        if example is None:
            unknown.append(example_id)
        else:
            selected.append(example)
    if unknown:
        valid = ", ".join(sorted(EXAMPLES_BY_ID))
        raise WGSExtractError(
            f"Unknown example ID(s): {', '.join(unknown)}. Valid IDs: {valid}"
        )
    return selected


def _target_root(override: str | None) -> Path:
    if override:
        root = Path(override).expanduser()
    elif settings.get("genome_library"):
        root = Path(settings["genome_library"]).expanduser()
    else:
        root = _repo_root() / "genomes"
    return root.resolve()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve_method(method: str, aspera_key: str | None = None) -> str:
    if method in {"auto", "ftp"}:
        return "ftp"
    if method == "aspera":
        if shutil.which("ascp") is None:
            raise WGSExtractError(
                "Aspera requested but 'ascp' is not installed. Use --method ftp."
            )
        if not _resolve_aspera_key(aspera_key):
            raise WGSExtractError(
                "Aspera requested but no private key was found. Pass --aspera-key "
                "or use --method ftp."
            )
        return "aspera"
    raise WGSExtractError(f"Unsupported transfer method: {method}")


def _planned_downloads(
    example: GenomeExample, example_dir: Path, method: str
) -> list[tuple[str, Path, str]]:
    return [
        (
            _source_for(file.url_path, method),
            example_dir / _filename(file.url_path),
            file.role,
        )
        for file in example.files
    ]


def _source_for(url_path: str, method: str) -> str:
    if method == "aspera":
        return f"{ASPERA_ROOT}/{url_path}"
    return f"{FTP_ROOT}/{url_path}"


def _filename(url_path: str) -> str:
    parsed = urlparse(url_path)
    name = Path(parsed.path).name
    if not name:
        raise WGSExtractError(f"Cannot determine filename from URL path: {url_path}")
    return name


def _download_file(
    source: str, destination: Path, method: str, aspera_key: str | None = None
) -> None:
    logging.info("Downloading %s", source)
    try:
        if method == "aspera":
            key = _resolve_aspera_key(aspera_key)
            if key is None:
                raise WGSExtractError("No Aspera private key available.")
            run_command(
                [
                    "ascp",
                    "-i",
                    str(key),
                    "-k",
                    "1",
                    "-T",
                    "-P",
                    "33001",
                    "-l",
                    "300M",
                    source,
                    str(destination),
                ]
            )
        else:
            run_command(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--continue-at",
                    "-",
                    "--output",
                    str(destination),
                    source,
                ]
            )
    except Exception as e:
        raise WGSExtractError(f"Download failed for {source}: {e}") from e


def _resolve_aspera_key(aspera_key: str | None) -> Path | None:
    candidates = []
    if aspera_key:
        candidates.append(Path(aspera_key).expanduser())
    candidates.extend(
        Path(path).expanduser()
        for path in (
            "~/.aspera/connect/etc/asperaweb_id_dsa.openssh",
            "/opt/aspera/connect/etc/asperaweb_id_dsa.openssh",
            "/usr/local/aspera/connect/etc/asperaweb_id_dsa.openssh",
        )
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _write_genome_config(example: GenomeExample, example_dir: Path) -> None:
    lines = [
        "# WGS Extract per-genome configuration",
        "# Downloaded from the 1000 Genomes Project example catalog.",
        f"# Example: {example.example_id} ({example.label})",
        "",
    ]
    for file in example.files:
        if file.role in CONFIG_ROLES:
            lines.append(f'{file.role} = "{_filename(file.url_path)}"')
    (example_dir / GENOME_CONFIG_NAME).write_text("\n".join(lines) + "\n")


def _print_plan(example: GenomeExample, planned: list[tuple[str, Path, str]]) -> None:
    print(f"{example.example_id}: {example.label}")
    for source, destination, role in planned:
        print(f"  {role}: {source}")
        print(f"        -> {destination}")
