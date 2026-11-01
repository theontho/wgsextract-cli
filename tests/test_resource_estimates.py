import argparse
import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.htslib_helpers import run_wgsextract
from wgsextract_cli.commands import align, bam, realign
from wgsextract_cli.commands.vcf import basic, callers
from wgsextract_cli.core import alignment_memory
from wgsextract_cli.core import resource_estimates as resources

GIB = 1024**3
H2_READS = 679_639_896


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


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "input.cram"
    source.write_bytes(b"x" * 1200)
    return source


def test_streamed_collation_spill_is_charged_to_selected_temporary_volume(
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
    assert "collation_spill" not in disks[output].components
    assert disks[scratch].components["collation_spill"] > 0
    assert disks[scratch].components["temporary"] > 0


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
    source = tmp_path / "source.bam"
    source.write_bytes(b"bam")
    commands = []

    def failed(*_a, **_k):
        raise error

    monkeypatch.setattr(bam, "estimate_resources", failed)
    monkeypatch.setattr(bam, "verify_dependencies", lambda *_: None)
    monkeypatch.setattr(bam, "log_dependency_info", lambda *_: None)
    monkeypatch.setattr(bam, "print_warning", lambda *_a, **_k: None)
    monkeypatch.setattr(
        bam, "get_base_args", lambda _: ("1", "256M", str(tmp_path), [], None)
    )
    monkeypatch.setattr(bam, "resolve_region_or_gene", lambda *_: None)
    monkeypatch.setattr(bam, "run_command", lambda cmd: commands.append(cmd))
    bam.cmd_tocram(argparse.Namespace(input=str(source), cram_version="3.1"))
    assert len(commands) == 2
    assert commands[0][:3] == ["samtools", "view", "-Ch"]
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
        (bam, bam.cmd_sort, "sort"),
        (bam, bam.cmd_tocram, "to-cram"),
        (bam, bam.cmd_tobam, "to-bam"),
        (basic, basic.cmd_snp, "snp"),
        (basic, basic.cmd_indel, "indel"),
        (callers, callers.cmd_gatk, "gatk"),
    ],
)
def test_commands_report_before_work(
    module, command, operation, tmp_path, source, monkeypatch
):
    monkeypatch.setattr(module, "verify_dependencies", lambda *_: None)
    monkeypatch.setattr(module, "log_dependency_info", lambda *_: None, raising=False)
    if module is bam:
        monkeypatch.setattr(
            module, "get_base_args", lambda _: ("1", "256M", str(tmp_path), [], None)
        )
        monkeypatch.setattr(module, "resolve_region_or_gene", lambda *_: None)
    else:
        monkeypatch.setattr(
            module, "get_base_args", lambda _: ("1", str(tmp_path), "", None)
        )

    def reported(build):
        estimate = build()
        assert estimate.operation == operation
        raise Reported

    monkeypatch.setattr(module, "preflight_resources", reported)
    args = argparse.Namespace(input=str(source), region=None, gpu=False)
    with pytest.raises(Reported):
        command(args)


@pytest.mark.parametrize("operation", ["align", "realign"])
@pytest.mark.parametrize("platform", ["illumina", "ultima"])
def test_alignment_reports_before_index_building_or_fastq_extraction(
    operation, platform, tmp_path, source, monkeypatch
):
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
            assert estimate.resources[1].components["collation_spill"] > 0
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
