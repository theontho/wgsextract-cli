from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.utils import WGSExtractError

HTTPS_ROOT = "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp"


ASPERA_ROOT = "fasp-g1k@fasp.1000genomes.ebi.ac.uk:/vol1/ftp"


ASPERA_PORT = "33001"  # EBI's public 1000 Genomes Aspera service port.


ASPERA_MAX_BANDWIDTH = "300M"


ASPERA_RESUME_PARTIAL = "1"


COLLECTION_DIR = "test-1000genomes"


@dataclass(frozen=True)
class ExampleFile:
    url_path: str
    role: str
    transfer_method: str | None = None


@dataclass(frozen=True)
class GenomeExample:
    example_id: str
    sample: str
    label: str
    data_type: str
    size: str
    description: str
    files: tuple[ExampleFile, ...]
    tags: tuple[str, ...] = ()


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
                "phase3/data/NA12878/sequence_read/ERR001268_1.filt.fastq.gz",
                "fastq_r1",
            ),
            ExampleFile(
                "phase3/data/NA12878/sequence_read/ERR001268_2.filt.fastq.gz",
                "fastq_r2",
            ),
        ),
    ),
    GenomeExample(
        example_id="na12878-lowcov-bam",
        sample="NA12878",
        label="NA12878 chromosome 20 low-coverage BAM",
        data_type="bam",
        size="single chromosome low coverage",
        description="Phase 3 chromosome 20 low-coverage GRCh37 alignment.",
        files=(
            ExampleFile(
                "phase3/data/NA12878/alignment/NA12878.chrom20.ILLUMINA.bwa.CEU.low_coverage.20121211.bam",
                "alignment",
            ),
            ExampleFile(
                "phase3/data/NA12878/alignment/NA12878.chrom20.ILLUMINA.bwa.CEU.low_coverage.20121211.bam.bai",
                "alignment_index",
            ),
        ),
    ),
    GenomeExample(
        example_id="na12878-exome-bam",
        sample="NA12878",
        label="NA12878 chromosome 20 exome BAM",
        data_type="bam",
        size="single chromosome exome",
        description="Phase 3 chromosome 20 exome alignment for faster alignment-file workflows.",
        files=(
            ExampleFile(
                "phase3/data/NA12878/exome_alignment/NA12878.chrom20.ILLUMINA.bwa.CEU.exome.20121211.bam",
                "alignment",
            ),
            ExampleFile(
                "phase3/data/NA12878/exome_alignment/NA12878.chrom20.ILLUMINA.bwa.CEU.exome.20121211.bam.bai",
                "alignment_index",
            ),
        ),
    ),
    GenomeExample(
        example_id="na12878-highcov-cram",
        sample="NA12878",
        label="NA12878 high-coverage CRAM",
        data_type="cram",
        size="high-coverage whole genome",
        description="High-coverage PCR-free GRCh37 CRAM for full-size workflow testing.",
        files=(
            ExampleFile(
                "phase3/data/NA12878/high_coverage_alignment/NA12878.mapped.ILLUMINA.bwa.CEU.high_coverage_pcr_free.20130906.bam.cram",
                "alignment",
            ),
            ExampleFile(
                "phase3/data/NA12878/high_coverage_alignment/NA12878.mapped.ILLUMINA.bwa.CEU.high_coverage_pcr_free.20130906.bam.cram.crai",
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
                "release/20130502/ALL.chr20.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf.gz",
                "vcf",
            ),
            ExampleFile(
                "release/20130502/ALL.chr20.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf.gz.tbi",
                "vcf_index",
            ),
        ),
    ),
    GenomeExample(
        example_id="hgsvc2-hg00733-pacbio-hifi-bam",
        sample="HG00733",
        label="HGSVC2 HG00733 PacBio HiFi CCS BAM",
        data_type="pacbio-hifi-bam",
        size="single PacBio movie, large (~9.5 GB)",
        description="HGSVC2/1000 Genomes PacBio HiFi CCS read BAM for PacBio alignment and variant-calling tests.",
        files=(
            ExampleFile(
                "data_collections/HGSVC2/working/20190925_PUR_PacBio_HiFi/HG00733_20190925_EEE_m54329U_190607_185248.ccs.bam",
                "fastq_r1",
            ),
            ExampleFile(
                "data_collections/HGSVC2/working/20190925_PUR_PacBio_HiFi/HG00733_20190925_EEE_m54329U_190607_185248.ccs.bam.pbi",
                "pacbio_pbi",
            ),
        ),
        tags=("1000g", "hgsvc2", "pacbio", "hifi", "bam"),
    ),
    GenomeExample(
        example_id="hgsvc2-hg00732-pacbio-hifi-bam-smallest",
        sample="HG00732",
        label="HGSVC2 HG00732 PacBio HiFi CCS BAM (smallest movie)",
        data_type="pacbio-hifi-bam",
        size="single PacBio movie, large (~3.5 GB)",
        description="Smallest HGSVC2/1000 Genomes PacBio HiFi CCS read BAM found in the index; useful as the first full real-data PacBio workflow target.",
        files=(
            ExampleFile(
                "https://ftp.sra.ebi.ac.uk/vol1/run/ERR386/ERR3861393/HG00732-hifi-r54329U_20190830_234003-B01.bam",
                "fastq_r1",
                "https",
            ),
        ),
        tags=("1000g", "hgsvc2", "pacbio", "hifi", "bam", "smallest"),
    ),
    GenomeExample(
        example_id="hgsvc2-hg00733-pacbio-hifi-fastq",
        sample="HG00733",
        label="HGSVC2 HG00733 PacBio HiFi Q20 FASTQ",
        data_type="pacbio-hifi-fastq",
        size="single PacBio movie, large (~9.3 GB)",
        description="HGSVC2/1000 Genomes PacBio HiFi Q20 FASTQ for minimap2/pbmm2 alignment tests.",
        files=(
            ExampleFile(
                "data_collections/HGSVC2/working/20190925_PUR_PacBio_HiFi/HG00733_20190925_EEE_m54329U_190607_185248.Q20.fastq.gz",
                "fastq_r1",
            ),
        ),
        tags=("1000g", "hgsvc2", "pacbio", "hifi", "fastq"),
    ),
    GenomeExample(
        example_id="hgsvc2-na19240-pacbio-hifi-bam",
        sample="NA19240",
        label="HGSVC2 NA19240 PacBio HiFi CCS BAM",
        data_type="pacbio-hifi-bam",
        size="single PacBio movie, large (~15 GB compressed reads)",
        description="HGSVC2/1000 Genomes PacBio HiFi CCS read BAM from the YRI trio for full PacBio workflow testing.",
        files=(
            ExampleFile(
                "data_collections/HGSVC2/HGSVC2_pacbio.index",
                "metadata",
            ),
            ExampleFile(
                "data_collections/HGSVC2/working/20191005_YRI_PacBio_NA19240_HiFi/NA19240_20191002_CLEE_m54336U_190827_013439.ccs.bam",
                "fastq_r1",
            ),
            ExampleFile(
                "data_collections/HGSVC2/working/20191005_YRI_PacBio_NA19240_HiFi/NA19240_20191002_CLEE_m54336U_190827_013439.ccs.bam.pbi",
                "pacbio_pbi",
            ),
        ),
        tags=("1000g", "hgsvc2", "pacbio", "hifi", "bam"),
    ),
)


EXAMPLES_BY_ID = {example.example_id: example for example in EXAMPLES}


CONFIG_ROLES = {"alignment", "vcf", "fastq_r1", "fastq_r2"}


def _filter_examples_by_tags(
    examples: tuple[GenomeExample, ...], tags: list[str] | None
) -> list[GenomeExample]:
    if not tags:
        return list(examples)
    wanted = {tag.strip().lower() for tag in tags if tag.strip()}
    return [example for example in examples if wanted.issubset(set(example.tags))]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _target_root(override: str | None) -> Path:
    """Return a canonical genome-library root, resolving symlinks for consistency."""
    if override:
        root = Path(override).expanduser()
    elif settings.get("genome_library"):
        root = Path(settings["genome_library"]).expanduser()
    else:
        root = _repo_root() / "genomes"
    return root.resolve()


def cmd_list(args: Namespace) -> None:
    """List available example genomes with metadata and usage instructions."""
    root = _target_root(args.target_root)
    print(f"Target collection: {root / COLLECTION_DIR}")
    print()
    for example in _filter_examples_by_tags(EXAMPLES, getattr(args, "tag", None)):
        print(f"{example.example_id}")
        print(f"  label: {example.label}")
        print(f"  type:  {example.data_type}")
        print(f"  size:  {example.size}")
        if example.tags:
            print(f"  tags:  {', '.join(example.tags)}")
        print(f"  files: {len(example.files)}")
        print(f"  use:   wgsextract --genome {COLLECTION_DIR}/{example.example_id} ...")
        print()


def _all_tags() -> list[str]:
    return sorted({tag for example in EXAMPLES for tag in example.tags})


def _select_examples(
    example_ids: list[str], include_all: bool, tags: list[str] | None = None
) -> list[GenomeExample]:
    if tags:
        if example_ids or include_all:
            raise WGSExtractError(
                "Use --tag by itself, not with --all or explicit example IDs."
            )
        selected = _filter_examples_by_tags(EXAMPLES, tags)
        if not selected:
            valid = ", ".join(_all_tags())
            raise WGSExtractError(
                f"No examples match tag(s): {', '.join(tags)}. Valid tags: {valid}"
            )
        return selected
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
