from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "smoke": {
        "coverage": 0.1,
        "full_size": False,
        "region": "chrM",
        "target_count": 250,
    },
    "standard": {
        "coverage": 1.0,
        "full_size": False,
        "region": None,
        "target_count": 10_000,
    },
    "200mb": {
        # The scaled reference produces ~104 MiB at 37x on current fake data.
        "coverage": 71.0,
        "full_size": False,
        "region": None,
        "target_count": 100_000,
    },
    "full": {
        "coverage": 1.0,
        "full_size": True,
        "region": None,
        "target_count": 250_000,
    },
}


EXCLUDED_OPERATIONS = "VEP, DeepVariant, Yleaf, Haplogrep"


PROGRESS_STEP_WIDTH = 68


DEFAULT_REAL_DATASET_URL = (
    "https://github.com/theontho/wgsextract-cli/releases/download/v0.1.0/"
    "wgsextract-benchmark-hg19-mini.zip"
)


DEFAULT_REAL_DATASET_SHA256 = (
    "ad0f8070dc5ca35c4a6de540493a81df082d160417f747ae68d9c098c110a9f6"
)


THOUSAND_GENOMES_FTP_ROOT = "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp"


ENA_FTP_ROOT = "https://ftp.sra.ebi.ac.uk/vol1"


@dataclass(frozen=True)
class BenchmarkToolSpec:
    name: str
    required: bool
    purpose: str


BENCHMARK_EXTERNAL_TOOLS: tuple[BenchmarkToolSpec, ...] = (
    BenchmarkToolSpec("samtools", True, "BAM/CRAM/FASTA indexing and conversion"),
    BenchmarkToolSpec("bcftools", True, "SNP/indel calling and VCF statistics"),
    BenchmarkToolSpec("bgzip", True, "Target and VCF compression"),
    BenchmarkToolSpec("tabix", True, "Compressed target and VCF indexing"),
    BenchmarkToolSpec("bwa", True, "Short-read alignment"),
    BenchmarkToolSpec(
        "sambamba",
        False,
        "BAM sort/index/view acceleration when available on non-macOS",
    ),
    BenchmarkToolSpec(
        "samblaster",
        False,
        "Duplicate-marking stage during alignment when available",
    ),
    BenchmarkToolSpec("fastp", False, "FASTQ read trimming and QC"),
    BenchmarkToolSpec("fastqc", False, "FASTQ quality-control reports"),
    BenchmarkToolSpec("freebayes", False, "Haplotype-based variant calling"),
)


@dataclass
class BenchmarkResult:
    name: str
    slug: str
    status: str
    seconds: float
    command: list[str]
    output_dir: str
    stdout_log: str | None = None
    stderr_log: str | None = None
    returncode: int | None = None
    expected_outputs: list[str] | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "PASS"


@dataclass(frozen=True)
class BenchmarkThreadPlan:
    label: str
    default_threads: int | None
    per_step_threads: dict[str, int]
    reason: str


@dataclass(frozen=True)
class BenchmarkRemoteFile:
    role: str
    url: str
    filename: str
    md5: str | None = None


@dataclass(frozen=True)
class BenchmarkDerivedAlignment:
    source_role: str
    output_filename: str
    index_filename: str
    subsample: str


@dataclass(frozen=True)
class BenchmarkDatasetSpec:
    tag: str
    dataset_id: str
    description: str
    build: str
    sample: str | None
    kind: str
    remote_files: tuple[BenchmarkRemoteFile, ...] = ()
    derived_alignment: BenchmarkDerivedAlignment | None = None
    default_region: str | None = None
    region_safe: bool = False
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class BenchmarkDataset:
    dataset_id: str
    description: str
    build: str
    root: Path
    ref: Path
    bam: Path
    bam_index: Path | None
    cram: Path | None
    cram_index: Path | None
    fastq_r1: Path | None
    fastq_r2: Path | None
    vcf: Path | None
    vcf_index: Path | None
    targets: Path | None
    targets_index: Path | None
    default_region: str | None
    region_safe: bool
    manifest: dict[str, Any]


REAL_BENCHMARK_DATASETS: dict[str, BenchmarkDatasetSpec] = {
    "real": BenchmarkDatasetSpec(
        tag="real",
        dataset_id="hg19-mini-hg00096",
        description="HG00096 real 1000 Genomes low-coverage mini benchmark dataset",
        build="hg19",
        sample="HG00096",
        kind="archive",
        default_region="20",
    ),
    "real-1x": BenchmarkDatasetSpec(
        tag="real-1x",
        dataset_id="1000g-hg00096-hs37d5-real-1x",
        description=(
            "HG00096 1000 Genomes Phase 3 low-coverage WGS, deterministically "
            "downsampled to approximately 1x mapped coverage"
        ),
        build="hg37",
        sample="HG00096",
        kind="direct",
        remote_files=(
            BenchmarkRemoteFile(
                "ref",
                f"{THOUSAND_GENOMES_FTP_ROOT}/technical/reference/phase2_reference_assembly_sequence/hs37d5.fa.gz",
                "hs37d5.fa.gz",
            ),
            BenchmarkRemoteFile(
                "ref_fai",
                f"{THOUSAND_GENOMES_FTP_ROOT}/technical/reference/phase2_reference_assembly_sequence/hs37d5.fa.gz.fai",
                "hs37d5.fa.gz.fai",
            ),
            BenchmarkRemoteFile(
                "ref_gzi",
                f"{THOUSAND_GENOMES_FTP_ROOT}/technical/reference/phase2_reference_assembly_sequence/hs37d5.fa.gz.gzi",
                "hs37d5.fa.gz.gzi",
            ),
            BenchmarkRemoteFile(
                "source_cram",
                f"{THOUSAND_GENOMES_FTP_ROOT}/phase3/data/HG00096/alignment/HG00096.mapped.ILLUMINA.bwa.GBR.low_coverage.20120522.bam.cram",
                "HG00096.mapped.ILLUMINA.bwa.GBR.low_coverage.20120522.bam.cram",
            ),
            BenchmarkRemoteFile(
                "source_cram_index",
                f"{THOUSAND_GENOMES_FTP_ROOT}/phase3/data/HG00096/alignment/HG00096.mapped.ILLUMINA.bwa.GBR.low_coverage.20120522.bam.cram.crai",
                "HG00096.mapped.ILLUMINA.bwa.GBR.low_coverage.20120522.bam.cram.crai",
            ),
        ),
        derived_alignment=BenchmarkDerivedAlignment(
            source_role="source_cram",
            output_filename="HG00096.real-1x.downsampled.cram",
            index_filename="HG00096.real-1x.downsampled.cram.crai",
            subsample="20260504.2255",
        ),
        region_safe=True,
        metadata={
            "source_project": "1000 Genomes Phase 3 low-coverage alignment",
            "source_alignment": (
                "HG00096.mapped.ILLUMINA.bwa.GBR.low_coverage.20120522.bam.cram"
            ),
            "source_mapped_bases": 13_911_883_215,
            "target_coverage": "~1x mapped coverage",
        },
    ),
    "real-30x": BenchmarkDatasetSpec(
        tag="real-30x",
        dataset_id="1000g-hg00096-grch38-real-30x",
        description="HG00096 1000 Genomes NYGC 30x GRCh38 high-coverage WGS CRAM",
        build="hg38",
        sample="HG00096",
        kind="direct",
        remote_files=(
            BenchmarkRemoteFile(
                "ref",
                f"{THOUSAND_GENOMES_FTP_ROOT}/technical/reference/GRCh38_reference_genome/GRCh38_full_analysis_set_plus_decoy_hla.fa",
                "GRCh38_full_analysis_set_plus_decoy_hla.fa",
            ),
            BenchmarkRemoteFile(
                "ref_fai",
                f"{THOUSAND_GENOMES_FTP_ROOT}/technical/reference/GRCh38_reference_genome/GRCh38_full_analysis_set_plus_decoy_hla.fa.fai",
                "GRCh38_full_analysis_set_plus_decoy_hla.fa.fai",
            ),
            BenchmarkRemoteFile(
                "bam",
                f"{ENA_FTP_ROOT}/run/ERR324/ERR3240114/HG00096.final.cram",
                "HG00096.final.cram",
                md5="d3354f61a055adfcfc988470bc507b2d",
            ),
            BenchmarkRemoteFile(
                "bam_index",
                f"{ENA_FTP_ROOT}/run/ERR324/ERR3240114/HG00096.final.cram.crai",
                "HG00096.final.cram.crai",
            ),
        ),
        region_safe=True,
        metadata={
            "source_project": "1000 Genomes 2504 high coverage",
            "run_id": "ERR3240114",
            "study_id": "ERP114329",
            "instrument": "Illumina NovaSeq 6000",
            "read_count": 768_049_130,
            "base_count": 115_207_369_500,
            "target_coverage": "30x WGS",
        },
    ),
}


def _read_fai(fai_path: Path) -> list[tuple[str, int]]:
    contigs: list[tuple[str, int]] = []
    with open(fai_path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                contigs.append((parts[0], int(parts[1])))
    return contigs


def _default_heavy_region(ref_path: Path) -> str | None:
    fai_path = Path(str(ref_path) + ".fai")
    if not fai_path.exists():
        return None
    contigs = _read_fai(fai_path)
    if not contigs:
        return None
    chrom, length = contigs[0]
    end = min(length, 100_000)
    if end < 1:
        return None
    return f"{chrom}:1-{end}"


def _chrom_only_region(region: str | None) -> str | None:
    if not region:
        return None
    chrom, _sep, _range_part = region.partition(":")
    return chrom or None


def _contig_length(ref_path: Path, chrom: str) -> int | None:
    fai_path = Path(str(ref_path) + ".fai")
    if not fai_path.exists():
        return None
    for contig, length in _read_fai(fai_path):
        if contig == chrom:
            return length
    return None


def _command_region(region: str | None, ref_path: Path) -> str | None:
    if not region:
        return None
    chrom, has_range, _range_part = region.partition(":")
    if has_range:
        return region
    length = _contig_length(ref_path, chrom)
    if length is None:
        return region
    return f"{chrom}:1-{length}"


def _command_label(command_args: list[str]) -> str | None:
    parts = []
    for arg in command_args:
        if arg.startswith("-"):
            break
        parts.append(arg)
    return " ".join(parts) if parts else None


def _name_with_command_label(
    name: str, command_args: list[str], command_label: str | None = None
) -> str:
    label = command_label or _command_label(command_args)
    return f"{name} [{label}]" if label else name
