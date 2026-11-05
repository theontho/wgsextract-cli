import argparse
import json
import logging
import platform as host_platform
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.htslib_helpers import needs_tools, run_wgsextract, sam_to_bam, write_fasta
from wgsextract_cli import api
from wgsextract_cli.commands import align, bam, realign
from wgsextract_cli.commands.vcf import basic, callers
from wgsextract_cli.core import alignment_memory
from wgsextract_cli.core import resource_estimates as resources

GIB = 1024**3
H2_READS = 679_639_896


def test_arm_deepvariant_inference_floor_survives_fewer_shards_and_small_regions(
    source, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(resources, "is_linux_arm64", lambda: True)
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 24 * GIB)
    options = {
        "input_paths": [str(source)],
        "output_dir": str(tmp_path),
        "region": "chr20:1-1000",
    }
    one = resources.estimate_resources("deepvariant", threads="1", **options)
    sixteen = resources.estimate_resources("deepvariant", threads="16", **options)
    assert one.resources[0].estimated_bytes == sixteen.resources[0].estimated_bytes
    assert one.resources[0].status == "LOW"
    with caplog.at_level(logging.INFO):
        resources.report_resources(one)
    assert "LOW" in caplog.text
    assert "inference memory floor" in caplog.text
    many = resources.estimate_resources("deepvariant", threads="32", **options)
    assert many.resources[0].estimated_bytes > sixteen.resources[0].estimated_bytes
    mitigated = resources.estimate_resources(
        "deepvariant", threads="8", inference_batch_size=256, **options
    )
    assert mitigated.resources[0].estimated_bytes < one.resources[0].estimated_bytes
    assert mitigated.resources[0].status == "OK"


# hs38DH: GRCh38's primary contigs, then 3,341 alt/decoy/HLA contigs (one here).
HS38_PRIMARY = {
    "chr1": 248956422, "chr2": 242193529, "chr3": 198295559, "chr4": 190214555,
    "chr5": 181538259, "chr6": 170805979, "chr7": 159345973, "chr8": 145138636,
    "chr9": 138394717, "chr10": 133797422, "chr11": 135086622,
    "chr12": 133275309, "chr13": 114364328, "chr14": 107043718,
    "chr15": 101991189, "chr16": 90338345, "chr17": 83257441, "chr18": 80373285,
    "chr19": 58617616, "chr20": 64444167, "chr21": 46709983, "chr22": 50818468,
    "chrX": 156040895, "chrY": 57227415, "chrM": 16569,
}  # fmt: skip
HS38DH = {**HS38_PRIMARY, "decoys": 129060516}


def _whole_genome(tmp_path):
    ref = tmp_path / "hs38DH.fa"
    ref.write_text(">x\n")
    Path(f"{ref}.fai").write_text(
        "".join(f"{name}\t{length}\t0\t60\t61\n" for name, length in HS38DH.items())
    )
    return resources.dv_calling_regions(HS38DH, None), ref


def test_dv_partitions_reproduce_the_173_whole_genome_run(tmp_path):
    regions, _ref = _whole_genome(tmp_path)
    # postprocess_variants' logged partitions: chr1-3, 4-7, 8-12, 13-20, chr21+.
    assert resources.dv_partition_bases(regions, 5) == [
        689445510, 701904766, 685692706, 700430089, 439873846
    ]  # fmt: skip
    assert resources.dv_partition_bases([("chr1", 0, 3)], 4) is None


def test_whole_genome_deepvariant_budgets_postprocess_on_x86(
    source, tmp_path, monkeypatch
):
    monkeypatch.setattr(resources, "is_linux_arm64", lambda: False)
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 64 * GIB)
    _regions, ref = _whole_genome(tmp_path)
    options = {
        "input_paths": [str(source)],
        "output_dir": str(tmp_path),
        "reference": str(ref),
        "threads": "5",
    }
    estimate = resources.estimate_resources("deepvariant", **options)
    stage = estimate.resources[0].components["largest_sequential_stage"]
    # #173 measured 25-30 GB; workers were killed before their peaks.
    assert 30 * GIB < stage < 40 * GIB
    assert any("5 workers over 5 partitions" in note for note in estimate.notes)
    regional = resources.estimate_resources("deepvariant", region="chr20", **options)
    assert regional.resources[0].components["largest_sequential_stage"] == 16 * GIB
    exome = resources.estimate_resources(
        "deepvariant", deepvariant_model="WES", **options
    )
    assert exome.resources[0].components["largest_sequential_stage"] == 16 * GIB


@pytest.mark.parametrize(
    ("threads", "available", "cpus", "partitions"),
    [
        (5, None, 5, 5),  # Unknown RAM: DeepVariant's default.
        (5, 64 * GIB, 5, 5),
        (5, 16 * GIB, 5, 19),  # More, smaller partitions keep every worker.
        (16, 12 * GIB, 10, 118),  # Fewer workers once partitions stop shrinking.
    ],
)
def test_postprocess_plan_fits_available_memory(
    tmp_path, threads, available, cpus, partitions
):
    regions, _ref = _whole_genome(tmp_path)
    plan = resources.plan_dv_postprocess(regions, threads, available)
    assert (plan.cpus, plan.partitions) == (cpus, partitions)
    assert plan.fits is (None if available is None else True)
    if available is not None:
        assert plan.peak_bytes <= available - GIB


def test_postprocess_plan_without_num_partitions_only_lowers_workers(tmp_path):
    regions, _ref = _whole_genome(tmp_path)
    # Fewer workers sort bigger partitions, so whole-genome RAM barely drops.
    plans = [
        resources.plan_dv_postprocess(regions, 5, gib * GIB, partitions=False)
        for gib in (16, 32, 40)
    ]
    assert [(p.cpus, p.partitions, p.fits) for p in plans] == [
        (1, 1, False),
        (1, 1, True),
        (5, 5, True),
    ]


def test_postprocess_plan_reports_when_one_worker_cannot_fit(tmp_path):
    regions, _ref = _whole_genome(tmp_path)
    plan = resources.plan_dv_postprocess(regions, 8, 1 * GIB)
    assert (plan.cpus, plan.fits) == (1, False)
    assert plan.worker_bytes > resources.DV_WORKER_BASE_BYTES


def test_realign_staged_fastqs_are_budgeted_on_the_temporary_volume(
    tmp_path, source, monkeypatch
):
    out = str(tmp_path / "out")
    spill = str(tmp_path / "spill")
    monkeypatch.setattr(resources, "_disk_space", lambda path: (path, 100 * GIB))
    estimate = resources.estimate_resources(
        "realign",
        input_paths=[str(source)],
        output_dir=out,
        temp_dir=spill,
        fastq_dir=spill,
        extract_fastq=True,
        reads=1000,
    )
    disks = {
        row.paths[0]: row
        for row in estimate.resources
        if row.resource == "disk" and row.paths
    }
    assert "extracted_fastqs" in disks[spill].components
    assert "extracted_fastqs" not in disks[out].components


def test_regional_gatk_and_scatter_artifacts_are_budgeted_on_the_selected_volume(
    monkeypatch,
):
    monkeypatch.setattr(resources, "_input_size", lambda _: 100 * 1024**2)
    monkeypatch.setattr(resources, "_disk_space", lambda path: (path, 100 * GIB))
    estimate = resources.estimate_resources(
        "gatk",
        input_paths=["input.cram"],
        output_dir="output",
        temp_dir="scratch",
        gatk_staging_dir="scratch",
        region="chrM",
        gatk_workers=2,
    )
    disks = {
        row.paths[0]: row
        for row in estimate.resources
        if row.resource == "disk" and row.paths
    }
    expected = {"regional_bam", "scatter_parts", "scatter_indexes"}
    assert expected <= disks["scratch"].components.keys()
    assert not expected & disks["output"].components.keys()


def _tiny_bam(tmp_path: Path) -> tuple[Path, Path]:
    """A one-read BAM on a 100 bp chr1, and its reference."""
    seq = "ACGT" * 25
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": seq})
    read = f"r1\t0\tchr1\t1\t60\t20M\t*\t0\t0\t{seq[:20]}\t{'I' * 20}"
    bam_path = tmp_path / "tiny.bam"
    sam_to_bam(["@HD\tVN:1.6\tSO:coordinate", "@SQ\tSN:chr1\tLN:100", read], bam_path)
    return bam_path, ref


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "input.cram"
    source.write_bytes(b"x" * 1200)
    return source


def test_streamed_collation_spill_drains_within_the_temporary_sort_estimate(
    tmp_path, source, monkeypatch
):
    output, scratch = str(tmp_path / "output"), str(tmp_path / "scratch")
    monkeypatch.setattr(resources, "_disk_space", lambda path: (path, 100 * GIB))
    estimate = resources.estimate_resources(
        "realign",
        input_paths=[str(source)],
        output_dir=output,
        temp_dir=scratch,
        reads=1000,
        additional_ram_bytes=64 * 1024**2,
    )
    disks = {
        row.paths[0]: row
        for row in estimate.resources
        if row.resource == "disk" and row.paths
    }
    # Collate's buckets are deleted as they drain while the sorter's spills grow.
    assert set(disks[scratch].components) == {"temporary"}
    assert "temporary" not in disks[output].components


def test_h2_envelope_reuses_memory_model_and_scales_disk_by_input_reads(
    tmp_path, source, monkeypatch
):
    reference = tmp_path / "ref.fa"
    reference.write_text(">chr1\nA\n")
    Path(str(reference) + ".fai").write_text("chr1\t3217346917\t0\t1\t2\n")
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 64 * GIB)
    monkeypatch.setattr(resources, "_disk_space", lambda _: ("same", 500 * GIB))
    options = {
        "input_paths": [str(source)],
        "output_dir": str(tmp_path),
        "reference": str(reference),
        "reads": H2_READS,
        "threads": "6",
        "memory": "256M",
        "output_format": "CRAM",
    }
    estimate = resources.estimate_resources("align", **options)
    doubled = resources.estimate_resources(
        "align", **{**options, "reads": H2_READS * 2}
    )
    expected = alignment_memory.estimate_mapping_memory(
        str(reference), "bwa", threads="6", memory="256M", reads=H2_READS
    )
    memory, disk = estimate.resources
    assert memory.estimated_bytes == expected.total
    assert 18 * GIB < expected.total < 19 * GIB
    assert 70e9 < disk.components["temporary"] < 90e9
    assert 16e9 < disk.components["output"] < 20e9
    assert (
        doubled.resources[1].components["temporary"] == disk.components["temporary"] * 2
    )
    assert doubled.resources[1].components["output"] == disk.components["output"] * 2
    assert doubled.resources[0].estimated_bytes > expected.total
    assert disk.components["reference_indexes"] > 0


def test_shared_volume_is_compared_to_combined_temp_output_and_index_need(
    tmp_path, source, monkeypatch, caplog
):
    monkeypatch.setattr(resources, "_disk_space", lambda _: ("C:", 90 * GIB))
    estimate = resources.estimate_resources(
        "align",
        input_paths=[str(source)],
        output_dir=str(tmp_path / "output"),
        temp_dir=str(tmp_path / "spill"),
        reads=H2_READS,
        output_format="CRAM",
    )
    memory, disk, unknown = estimate.resources
    assert unknown.status == "UNKNOWN" and not unknown.paths
    assert disk.components["temporary"] < 90 * GIB
    assert disk.components["output"] < 90 * GIB
    assert disk.status == "LOW"
    with caplog.at_level(logging.INFO):
        resources.report_resources(estimate)
    assert "Preflight disk   LOW" in caplog.text
    assert str(tmp_path / "spill") in caplog.text
    assert memory.resource == "memory"


def test_separate_volumes_are_budgeted_independently(tmp_path, source, monkeypatch):
    out = str(tmp_path / "output")
    spill = str(tmp_path / "spill")
    monkeypatch.setattr(
        resources,
        "_disk_space",
        lambda path: ("C:", 20 * GIB) if path == out else ("E:", 80 * GIB),
    )
    estimate = resources.estimate_resources(
        "align",
        input_paths=[str(source)],
        output_dir=out,
        temp_dir=spill,
        reads=H2_READS,
        output_format="CRAM",
    )
    disks = [row for row in estimate.resources if row.resource == "disk" and row.paths]
    assert len(disks) == 2 and all(row.status == "OK" for row in disks)
    assert {tuple(row.components) for row in disks} == {
        ("output", "indexes"),
        ("temporary",),
    }


def test_gui_api_is_json_serializable_and_does_not_create_output_directories(
    source, tmp_path, monkeypatch
):
    asked = []
    monkeypatch.setattr(
        resources.shutil,
        "disk_usage",
        lambda path: asked.append(path) or SimpleNamespace(free=100 * GIB),
    )
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 2 * GIB)
    output = tmp_path / "missing" / "nested"
    estimate = resources.estimate_resources(
        "to-bam",
        input_paths=[str(source)],
        output_dir=str(output),
        threads="2",
    )
    encoded = json.loads(json.dumps(estimate.to_dict()))
    assert encoded["resources"][1]["components"]["output"] == 10_000
    assert encoded["resources"][0]["status"] == "OK"
    assert asked == [tmp_path.resolve(), tmp_path.resolve()]
    assert not output.exists()


def test_unavailable_measurements_are_unknown_not_false_ok(tmp_path, monkeypatch):
    def unreadable(_path):
        raise OSError("unavailable")

    monkeypatch.setattr(resources.shutil, "disk_usage", unreadable)
    estimate = resources.estimate_resources(
        "sort", input_paths=[str(tmp_path / "missing.cram")], output_dir=str(tmp_path)
    )
    assert estimate.resources[1].status == "UNKNOWN"
    assert estimate.resources[1].estimated_bytes is None
    assert estimate.resources[1].available_bytes is None


def test_unresolved_reference_has_a_structured_unknown_disk_row(tmp_path, source):
    estimate = resources.estimate_resources(
        "align", input_paths=[str(source)], output_dir=str(tmp_path), reads=100
    )
    unknown = [
        row for row in estimate.resources if row.resource == "disk" and not row.paths
    ]
    assert len(unknown) == 1
    assert unknown[0].status == "UNKNOWN"
    assert unknown[0].estimated_bytes is None
    assert unknown[0].available_bytes is None


@pytest.mark.parametrize("operation", ["align", "realign"])
def test_gatk_duplicate_marking_budgets_two_retained_bams(
    tmp_path, source, monkeypatch, operation
):
    reads = 1_000_000
    monkeypatch.setattr(resources, "_disk_space", lambda _: ("same", 180_000_000))
    estimate = resources.estimate_resources(
        operation,
        input_paths=[str(source)],
        output_dir=str(tmp_path),
        reads=reads,
        output_format="CRAM",
        samblaster=False,
        duplicate_marking="gatk-flow",
    )
    disk = estimate.resources[1]
    assert disk.components["temporary"] >= reads * resources.BAM_BYTES_PER_READ * 2
    assert disk.status == "LOW"


@pytest.mark.parametrize(
    "options", [{"threads": "bad"}, {"memory": "bad"}, {"reads": -1}]
)
def test_gui_estimator_remains_strict(tmp_path, source, options):
    with pytest.raises(ValueError):
        resources.estimate_resources(
            "sort", input_paths=[str(source)], output_dir=str(tmp_path), **options
        )


@needs_tools("samtools")
@pytest.mark.parametrize(
    "error",
    [
        ValueError("invalid estimate"),
        OSError("metadata unavailable"),
        subprocess.CalledProcessError(1, ["metadata"]),
    ],
)
def test_command_proceeds_when_advisory_estimation_fails(
    tmp_path, monkeypatch, caplog, error
):
    source, ref = _tiny_bam(tmp_path)

    def failed(*_a, **_k):
        raise error

    monkeypatch.setattr(bam, "estimate_resources", failed)
    outdir = tmp_path / "out"
    result = api.bam.to_cram(
        api.BamToCramParams(input=source, ref=ref, outdir=outdir, cram_version="3.1")
    )
    assert result.status == "ok"
    assert (outdir / "tiny.cram").stat().st_size > 0
    assert "resource estimate unavailable:" in caplog.text


def test_realign_includes_extracted_fastqs_only_when_needed(tmp_path, source):
    options = {
        "input_paths": [str(source)],
        "output_dir": str(tmp_path),
        "reads": 100_000,
        "output_format": "CRAM",
    }
    extract = resources.estimate_resources("realign", **options, extract_fastq=True)
    reuse = resources.estimate_resources("realign", **options)
    assert (
        extract.resources[1].estimated_bytes - reuse.resources[1].estimated_bytes
        == 7_500_000
    )
    assert extract.resources[0].estimated_bytes == reuse.resources[0].estimated_bytes


def test_disk_fallback_grows_with_input_bytes_for_the_same_reference(tmp_path, source):
    options = {"input_paths": [str(source)], "output_dir": str(tmp_path)}
    small = resources.estimate_resources("sort", **options)
    source.write_bytes(source.read_bytes() * 2)
    large = resources.estimate_resources("sort", **options)
    for component in ("temporary", "output"):
        assert (
            large.resources[1].components[component]
            == small.resources[1].components[component] * 2
        )
    assert large.resources[0].estimated_bytes == small.resources[0].estimated_bytes


def test_variant_worker_memory_and_regional_gatk_conversion(tmp_path, source):
    reference = tmp_path / "ref.fa"
    reference.write_text(">chr1\nA\n")
    Path(str(reference) + ".fai").write_text("chr1\t10\t0\t1\t2\nchr2\t10\t0\t1\t2\n")
    options = {
        "input_paths": [str(source)],
        "output_dir": str(tmp_path),
        "reference": str(reference),
        "threads": "6",
    }
    serial = resources.estimate_resources("snp", **options)
    full = resources.estimate_resources("snp", **options, parallel=True)
    region = resources.estimate_resources("indel", **options, region="chr1:1-10")
    assert full.resources[0].components["callers"] == 2 * GIB
    assert serial.resources[0].components["callers"] == GIB
    assert region.resources[0].components["callers"] == GIB
    gatk = resources.estimate_resources("gatk", **options, region="chr1:1-10")
    assert gatk.resources[1].components["regional_bam"] > 0
    assert any("whole input" in note for note in gatk.notes)


class Reported(Exception):
    pass


@pytest.mark.parametrize(
    ("module", "command", "operation"),
    [
        pytest.param(bam, bam.cmd_sort, "sort", marks=needs_tools("samtools")),
        pytest.param(bam, bam.cmd_tocram, "to-cram", marks=needs_tools("samtools")),
        pytest.param(bam, bam.cmd_tobam, "to-bam", marks=needs_tools("samtools")),
        (basic, basic.cmd_snp, "snp"),
        (basic, basic.cmd_indel, "indel"),
        (callers, callers.cmd_gatk, "gatk"),
    ],
)
def test_commands_report_before_work(
    module, command, operation, tmp_path, source, monkeypatch
):
    monkeypatch.setattr(module, "verify_dependencies", lambda *_: None)
    if operation == "gatk":
        monkeypatch.setattr(module, "require_native_tool", lambda _tool: None)
    monkeypatch.setattr(module, "log_dependency_info", lambda *_: None, raising=False)
    if module is bam:
        source, _ref = _tiny_bam(tmp_path)
    else:
        setup = basic.CallerSetup("1", str(tmp_path), "", None, None)  # type: ignore[arg-type]
        monkeypatch.setattr(module, "setup_caller", lambda _params: setup)

    def reported(build):
        estimate = build()
        assert estimate.operation == operation
        raise Reported

    monkeypatch.setattr(module, "preflight_resources", reported)
    outdir = tmp_path / "out"
    outdir.mkdir()
    args = argparse.Namespace(
        input=str(source), region=None, gpu=False, outdir=str(outdir)
    )
    with pytest.raises(Reported):
        command(args)


@pytest.mark.parametrize("operation", ["align", "realign"])
@pytest.mark.parametrize("platform", ["illumina", "ultima"])
def test_alignment_reports_before_index_building_or_fastq_extraction(
    operation, platform, tmp_path, source, monkeypatch
):
    monkeypatch.setattr(host_platform, "machine", lambda: "x86_64")
    ref = tmp_path / "ref.fa"
    ref.write_text(">chr1\nA\n")

    monkeypatch.setattr(
        align, "get_tool_path", lambda name: "gatk" if name == "gatk" else None
    )

    def reported(build):
        estimate = build()
        assert estimate.operation == operation
        if operation == "realign" and platform != "ultima":
            assert "extracted_fastqs" not in estimate.resources[1].components
            assert "collation_spill" not in estimate.resources[1].components
            assert estimate.resources[1].components["temporary"] > 0
            assert estimate.resources[0].components["collation"] > 0
        if platform == "ultima":
            assert estimate.resources[1].components["temporary"] == 20_000
        raise Reported

    if operation == "align":
        monkeypatch.setattr(
            align,
            "_prepare_alignment",
            lambda *_a, **_k: align._AlignSetup(
                "1", "256M", str(tmp_path), str(ref), "sample"
            ),
        )
        monkeypatch.setattr(
            align,
            "fastq_input_statistics",
            lambda *_: SimpleNamespace(reads=100, read_length=150),
        )
        monkeypatch.setattr(align, "preflight_resources", reported)
        monkeypatch.setattr(
            align, "ensure_bwa_index", lambda *_: pytest.fail("indexing started")
        )
        with pytest.raises(Reported):
            align._bwa_setup(
                argparse.Namespace(
                    r1="r1.fq", r2=None, aligner="bwa", platform=platform, format="CRAM"
                ),
                "align",
            )
    else:
        monkeypatch.setattr(align, "streaming_mapping_fits", lambda *_a, **_kw: True)
        monkeypatch.setattr(realign, "resolve_reference", lambda *_: str(ref))
        monkeypatch.setattr(realign, "indexed_read_count", lambda *_: 100)
        monkeypatch.setattr(realign, "_input_cram_reference", lambda *_: [])
        monkeypatch.setattr(realign, "preflight_resources", reported)
        monkeypatch.setattr(
            realign,
            "alignments_to_fastq",
            lambda *_a, **_k: pytest.fail("extraction started"),
        )
        with pytest.raises(Reported):
            run_wgsextract(
                "realign",
                "--input",
                str(source),
                "--ref",
                str(ref),
                "--aligner",
                "bwa",
                "--platform",
                platform,
                "--format",
                "CRAM",
                "--threads",
                "1",
                "--no-preflight",
            )
