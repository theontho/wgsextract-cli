from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

from tests.smoke_utils import check_tool, run_cli, verify_bam, verify_fastq, verify_vcf
from wgsextract_cli.core.microarray_utils import _resolve_templates_root


@dataclass(frozen=True)
class RealDataset:
    bam: Path
    vcf: Path
    ref: Path
    target_tab: Path | None
    fastq_r1: Path | None
    fastq_r2: Path | None
    cram: Path | None


def _existing_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.exists() else None


def _first_existing(paths: list[Path]) -> Path | None:
    return next((path for path in paths if path.exists()), None)


def _latest_benchmark_dataset() -> Path | None:
    run_roots = [
        Path("out/ci-real-data/benchmark/runs"),
        Path("out/benchmark/runs"),
    ]
    candidates: list[Path] = []
    for root in run_roots:
        if not root.exists():
            continue
        candidates.extend(root.glob("*/dataset/real/hg19-mini-hg00096"))

    existing = [path for path in candidates if path.is_dir()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def _dataset_from_env() -> RealDataset | None:
    bam = _existing_path(os.environ.get("WGSE_INPUT"))
    vcf = _existing_path(os.environ.get("WGSE_INPUT_VCF"))
    ref = _existing_path(os.environ.get("WGSE_REF"))
    if not bam or not vcf or not ref:
        return None

    return RealDataset(
        bam=bam,
        vcf=vcf,
        ref=ref,
        target_tab=_existing_path(os.environ.get("WGSE_REF_VCF_TAB")),
        fastq_r1=_existing_path(os.environ.get("WGSE_FASTQ_R1")),
        fastq_r2=_existing_path(os.environ.get("WGSE_FASTQ_R2")),
        cram=bam if bam.suffix == ".cram" else None,
    )


def _dataset_from_benchmark_cache() -> RealDataset | None:
    dataset_dir = _latest_benchmark_dataset()
    if not dataset_dir:
        return None

    bam = _first_existing(list(dataset_dir.glob("*.bam")))
    vcf = _first_existing(list(dataset_dir.glob("*.vcf.gz")))
    ref = _first_existing(
        list(dataset_dir.glob("*.fa.gz")) + list(dataset_dir.glob("*.fa"))
    )
    if not bam or not vcf or not ref:
        return None

    fastqs = sorted(dataset_dir.glob("*_R[12].fastq.gz"))
    return RealDataset(
        bam=bam,
        vcf=vcf,
        ref=ref,
        target_tab=_first_existing(list(dataset_dir.glob("*.targets.tab.gz"))),
        fastq_r1=fastqs[0] if len(fastqs) >= 2 else None,
        fastq_r2=fastqs[1] if len(fastqs) >= 2 else None,
        cram=_first_existing(list(dataset_dir.glob("*.cram"))),
    )


@pytest.fixture(scope="session")
def real_dataset() -> RealDataset:
    dataset = _dataset_from_env() or _dataset_from_benchmark_cache()
    if dataset is None:
        pytest.skip(
            "real smoke data not configured; set WGSE_INPUT/WGSE_INPUT_VCF/WGSE_REF "
            "or run the real benchmark smoke first"
        )
    return dataset


def _run_ok(args: list[str]) -> tuple[str, str]:
    rc, stdout, stderr = run_cli(args)
    assert rc == 0, (
        f"CLI command failed:\nargs={args!r}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )
    return stdout, stderr


def _reference_fai(ref: Path) -> Path | None:
    fasta = _reference_fasta(ref)
    if fasta is None:
        return None
    fai = Path(f"{fasta}.fai")
    return fai if fai.exists() else None


def _reference_fasta(ref: Path) -> Path | None:
    if ref.is_file():
        return ref
    if not ref.is_dir():
        return None

    candidates: list[Path] = []
    for subdir in ("genomes", "ref", ""):
        root = ref / subdir if subdir else ref
        if root.is_dir():
            candidates.extend(root.glob("*.fa"))
            candidates.extend(root.glob("*.fa.gz"))
            candidates.extend(root.glob("*.fasta"))
            candidates.extend(root.glob("*.fasta.gz"))
    indexed = [
        candidate for candidate in candidates if Path(f"{candidate}.fai").exists()
    ]
    return sorted(indexed or candidates)[0] if candidates else None


def _reference_contigs(ref: Path) -> list[str]:
    fai = _reference_fai(ref)
    if not fai:
        return []
    contigs: list[str] = []
    with fai.open() as handle:
        for line in handle:
            if line.strip():
                contigs.append(line.split("\t", 1)[0])
    return contigs


def _reference_region(ref: Path, *preferred: str) -> str:
    override = os.environ.get("WGSE_REGION")
    if override:
        return override

    contigs = _reference_contigs(ref)
    if not contigs:
        pytest.skip(f"reference index missing for {ref}")

    for contig in preferred:
        if contig in contigs:
            return contig
    return contigs[0]


def _target_tab(dataset: RealDataset) -> Path:
    if dataset.target_tab and dataset.target_tab.exists():
        return dataset.target_tab

    for root in (dataset.vcf.parent, dataset.ref.parent, dataset.bam.parent):
        matches = sorted(root.glob("*.targets.tab.gz"))
        if matches:
            return matches[0]
    pytest.skip("microarray target tab not found; set WGSE_REF_VCF_TAB")


def _microarray_template_roots(dataset: RealDataset) -> list[str]:
    return [str(dataset.ref.parent), str(_target_tab(dataset).parent)]


def _require_microarray_templates(dataset: RealDataset) -> None:
    if not _resolve_templates_root(_microarray_template_roots(dataset)):
        pytest.skip("microarray raw_file_templates not available for real smoke data")


def _single_file(outdir: Path, pattern: str) -> Path:
    matches = sorted(outdir.glob(pattern))
    assert matches, f"missing output matching {pattern} in {outdir}"
    return matches[0]


def _assert_combined_kit_has_calls(path: Path) -> None:
    valid_calls = 0
    with path.open() as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 4 and len(parts[3]) == 2 and set(parts[3]) <= set("ACGT"):
                valid_calls += 1
                break
    assert valid_calls > 0, f"no concrete genotype calls found in {path}"


def _samtools_count(bam: Path, region: str, ref: Path) -> int:
    if not check_tool("samtools"):
        pytest.skip("samtools missing")
    command = ["samtools", "view", "-c"]
    if bam.suffix == ".cram":
        fasta = _reference_fasta(ref)
        if fasta is None:
            pytest.skip(f"reference FASTA not resolvable for CRAM input: {bam}")
        command.extend(["-T", str(fasta)])
    command.extend([str(bam), region])
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        pytest.skip(f"samtools count timed out for {bam} {region}")
    except subprocess.CalledProcessError as exc:
        pytest.skip(
            f"samtools count failed for {bam} {region}: "
            f"{(exc.stderr or exc.stdout or '').strip()}"
        )
    return int(result.stdout.strip())


@cache
def _tool_unusable(tool: str, *args: str) -> bool:
    if not check_tool(tool):
        return True
    try:
        result = subprocess.run(
            [tool, *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return True
    return result.returncode != 0


def test_real_vcf_trio(real_dataset: RealDataset, tmp_path: Path) -> None:
    child = _existing_path(os.environ.get("WGSE_VCF_CHILD")) or real_dataset.vcf
    mother = _existing_path(os.environ.get("WGSE_VCF_MOTHER")) or real_dataset.vcf
    father = _existing_path(os.environ.get("WGSE_VCF_FATHER")) or real_dataset.vcf

    _run_ok(
        [
            "vcf",
            "trio",
            "--proband",
            str(child),
            "--mother",
            str(mother),
            "--father",
            str(father),
            "--outdir",
            str(tmp_path),
        ]
    )

    assert verify_vcf(str(tmp_path / "trio_denovo.vcf.gz"), allow_empty=True)


def test_real_vcf_microarray(real_dataset: RealDataset, tmp_path: Path) -> None:
    _require_microarray_templates(real_dataset)

    _run_ok(
        [
            "microarray",
            "--input",
            str(real_dataset.vcf),
            "--ref",
            str(real_dataset.ref),
            "--ref-vcf-tab",
            str(_target_tab(real_dataset)),
            "--outdir",
            str(tmp_path),
            "--formats",
            "23andme_v5,ancestry_v2",
        ]
    )

    combined_kit = _single_file(tmp_path, "*_CombinedKit.txt")
    _assert_combined_kit_has_calls(combined_kit)
    assert _single_file(tmp_path, "*_23andMe_V5.txt").exists()
    assert _single_file(tmp_path, "*_Ancestry_V2.txt").exists()


def test_real_bam_microarray(real_dataset: RealDataset, tmp_path: Path) -> None:
    _require_microarray_templates(real_dataset)

    _run_ok(
        [
            "microarray",
            "--input",
            str(real_dataset.cram or real_dataset.bam),
            "--ref",
            str(real_dataset.ref),
            "--ref-vcf-tab",
            str(_target_tab(real_dataset)),
            "--outdir",
            str(tmp_path),
            "--parallel",
            "--formats",
            "23andme_v5,ancestry_v2",
        ]
    )

    combined_kit = _single_file(tmp_path, "*_CombinedKit.txt")
    _assert_combined_kit_has_calls(combined_kit)


@pytest.mark.skipif(not check_tool("bwa"), reason="bwa missing")
def test_real_fastq_alignment(real_dataset: RealDataset, tmp_path: Path) -> None:
    if not real_dataset.fastq_r1 or not real_dataset.fastq_r2:
        pytest.skip("real FASTQ pair not configured")

    assert verify_fastq(str(real_dataset.fastq_r1))
    assert verify_fastq(str(real_dataset.fastq_r2))

    _run_ok(
        [
            "align",
            "--r1",
            str(real_dataset.fastq_r1),
            "--r2",
            str(real_dataset.fastq_r2),
            "--ref",
            str(real_dataset.ref),
            "--outdir",
            str(tmp_path),
            "--sample",
            "SmokeSample",
        ]
    )

    aligned = _single_file(tmp_path, "*.bam")
    assert verify_bam(str(aligned))


@pytest.mark.skipif(not check_tool("freebayes"), reason="freebayes missing")
def test_real_variant_calling(real_dataset: RealDataset, tmp_path: Path) -> None:
    region = _reference_region(real_dataset.ref, "20", "chr20", "MT", "chrM")

    _run_ok(
        [
            "vcf",
            "freebayes",
            "--input",
            str(real_dataset.bam),
            "--ref",
            str(real_dataset.ref),
            "--outdir",
            str(tmp_path / "freebayes"),
            "--region",
            region,
        ]
    )
    assert verify_vcf(
        str(tmp_path / "freebayes" / "freebayes.vcf.gz"), allow_empty=True
    )

    if check_tool("gatk"):
        _run_ok(
            [
                "vcf",
                "gatk",
                "--input",
                str(real_dataset.bam),
                "--ref",
                str(real_dataset.ref),
                "--outdir",
                str(tmp_path / "gatk"),
                "--region",
                region,
            ]
        )
        assert verify_vcf(str(tmp_path / "gatk" / "gatk.vcf.gz"), allow_empty=True)

    if check_tool("deepvariant"):
        _run_ok(
            [
                "vcf",
                "deepvariant",
                "--input",
                str(real_dataset.bam),
                "--ref",
                str(real_dataset.ref),
                "--outdir",
                str(tmp_path / "deepvariant"),
                "--region",
                region,
            ]
        )
        assert verify_vcf(
            str(tmp_path / "deepvariant" / "deepvariant.vcf.gz"),
            allow_empty=True,
        )


def _delly_region(real_dataset: RealDataset) -> str:
    region = _reference_region(real_dataset.ref, "20", "chr20", "Y", "chrY")
    if _samtools_count(real_dataset.bam, region, real_dataset.ref) < 1000:
        pytest.skip(f"not enough reads in {region} for Delly library estimation")
    return region


@pytest.mark.skipif(_tool_unusable("delly", "--version"), reason="delly unavailable")
def test_real_structural_variant_calling(
    real_dataset: RealDataset, tmp_path: Path
) -> None:
    region = _delly_region(real_dataset)
    _run_ok(
        [
            "vcf",
            "sv",
            "--input",
            str(real_dataset.bam),
            "--ref",
            str(real_dataset.ref),
            "--outdir",
            str(tmp_path / "sv"),
            "--region",
            region,
        ]
    )
    assert verify_vcf(str(tmp_path / "sv" / "sv.vcf.gz"), allow_empty=True)


@pytest.mark.skipif(_tool_unusable("delly", "--version"), reason="delly unavailable")
def test_real_cnv_calling(real_dataset: RealDataset, tmp_path: Path) -> None:
    region = _delly_region(real_dataset)
    cnv_map = _existing_path(os.environ.get("WGSE_CNV_MAP"))
    if not cnv_map:
        pytest.skip("WGSE_CNV_MAP not configured")

    _run_ok(
        [
            "vcf",
            "cnv",
            "--input",
            str(real_dataset.bam),
            "--ref",
            str(real_dataset.ref),
            "--outdir",
            str(tmp_path / "cnv"),
            "--region",
            region,
            "--map",
            str(cnv_map),
        ]
    )
    assert verify_vcf(str(tmp_path / "cnv" / "cnv.vcf.gz"), allow_empty=True)


def test_real_mito_ydna_bam_extract(real_dataset: RealDataset, tmp_path: Path) -> None:
    contigs = set(_reference_contigs(real_dataset.ref))
    if contigs and not contigs.intersection({"MT", "chrM", "M"}):
        pytest.skip("reference has no mitochondrial contig")
    if contigs and not contigs.intersection({"Y", "chrY"}):
        pytest.skip("reference has no Y contig")

    _run_ok(
        [
            "extract",
            "mt-bam",
            "--input",
            str(real_dataset.bam),
            "--ref",
            str(real_dataset.ref),
            "--outdir",
            str(tmp_path / "mt"),
        ]
    )
    assert verify_bam(str(_single_file(tmp_path / "mt", "*_mtDNA.bam")))

    _run_ok(
        [
            "extract",
            "ydna-bam",
            "--input",
            str(real_dataset.bam),
            "--ref",
            str(real_dataset.ref),
            "--outdir",
            str(tmp_path / "y"),
        ]
    )
    assert verify_bam(str(_single_file(tmp_path / "y", "*_Y.bam")), allow_empty=True)


@pytest.mark.skipif(not check_tool("haplogrep"), reason="haplogrep missing")
def test_real_mt_lineage(real_dataset: RealDataset, tmp_path: Path) -> None:
    _run_ok(
        [
            "lineage",
            "mt-haplogroup",
            "--input",
            str(real_dataset.bam),
            "--ref",
            str(real_dataset.ref),
            "--outdir",
            str(tmp_path),
        ]
    )
    assert (tmp_path / "haplogrep_results.txt").exists()


@pytest.mark.skipif(not check_tool("yleaf"), reason="yleaf missing")
def test_real_y_lineage(real_dataset: RealDataset, tmp_path: Path) -> None:
    _run_ok(
        [
            "lineage",
            "y-haplogroup",
            "--input",
            str(real_dataset.vcf),
            "--ref",
            str(real_dataset.ref),
            "--outdir",
            str(tmp_path),
        ]
    )
    _single_file(tmp_path, "**/*_Final_Report.txt")


@pytest.mark.skipif(not check_tool("vep"), reason="vep missing")
def test_real_vep(real_dataset: RealDataset, tmp_path: Path) -> None:
    vep_cache = Path.home() / ".vep"
    if not (vep_cache / "homo_sapiens").exists():
        pytest.skip(f"VEP cache missing at {vep_cache / 'homo_sapiens'}")

    _run_ok(
        [
            "vep",
            "--input",
            str(real_dataset.vcf),
            "--ref",
            str(real_dataset.ref),
            "--outdir",
            str(tmp_path),
            "--vep-cache",
            str(vep_cache),
            "--vep-assembly",
            os.environ.get("WGSE_VEP_ASSEMBLY", "GRCh38"),
        ]
    )
    assert verify_vcf(str(_single_file(tmp_path, "*vep*.vcf.gz")), allow_empty=True)


def test_real_reference_library_robustness(
    real_dataset: RealDataset, tmp_path: Path
) -> None:
    if not real_dataset.ref.is_dir():
        pytest.skip("standard reference robustness checks require a reference library")

    stdout, stderr = _run_ok(["ref", "library-list", "--ref", str(real_dataset.ref)])
    assert any(
        build in stdout + stderr for build in ("hg19", "hg38", "GRCh37", "GRCh38")
    )

    stdout, stderr = _run_ok(
        [
            "info",
            "--input",
            str(real_dataset.bam),
            "--ref",
            str(real_dataset.ref),
            "--outdir",
            str(tmp_path),
        ]
    )
    assert any(
        build in stdout + stderr for build in ("hg19", "hg38", "GRCh37", "GRCh38")
    )


def test_real_clinical_annotation_stack(
    real_dataset: RealDataset, tmp_path: Path
) -> None:
    if not real_dataset.ref.is_dir():
        pytest.skip("clinical annotation stack requires a reference library")

    resource_patterns = [
        "clinvar_*.vcf.gz",
        "gnomad_*",
        "revel_*.tsv.gz",
        "alphamissense_*",
        "pharmgkb_*",
    ]
    resource_root = real_dataset.ref / "ref"
    missing = [
        pattern
        for pattern in resource_patterns
        if next(resource_root.glob(pattern), None) is None
    ]
    if missing:
        pytest.skip(
            f"clinical annotation resources missing in {resource_root}: "
            f"{', '.join(missing)}"
        )

    steps = [
        ("clinvar", real_dataset.vcf, "clinvar_annotated.vcf.gz"),
        ("gnomad", tmp_path / "clinvar_annotated.vcf.gz", "gnomad_annotated.vcf.gz"),
        ("revel", tmp_path / "gnomad_annotated.vcf.gz", "revel_annotated.vcf.gz"),
        (
            "alphamissense",
            tmp_path / "revel_annotated.vcf.gz",
            "alphamissense_annotated.vcf.gz",
        ),
        (
            "pharmgkb",
            tmp_path / "alphamissense_annotated.vcf.gz",
            "pharmgkb_annotated.vcf.gz",
        ),
    ]

    for command, input_vcf, output_name in steps:
        _run_ok(
            [
                "vcf",
                command,
                "--input",
                str(input_vcf),
                "--ref",
                str(real_dataset.ref),
                "--outdir",
                str(tmp_path),
            ]
        )
        assert verify_vcf(str(tmp_path / output_name), allow_empty=True)
