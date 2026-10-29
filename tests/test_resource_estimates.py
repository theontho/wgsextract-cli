import argparse
import json
import logging
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


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "input.cram"
    source.write_bytes(b"x" * 1200)
    return source


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
    memory, disk = estimate.resources
    assert len(estimate.resources) == 2
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
    disks = [row for row in estimate.resources if row.resource == "disk"]
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
    full = resources.estimate_resources("snp", **options)
    region = resources.estimate_resources("indel", **options, region="chr1:1-10")
    assert full.resources[0].components["callers"] == 2 * GIB
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

    def reported(estimate):
        assert estimate.operation == operation
        raise Reported

    monkeypatch.setattr(module, "report_resources", reported)
    args = argparse.Namespace(input=str(source), region=None, gpu=False)
    with pytest.raises(Reported):
        command(args)


@pytest.mark.parametrize("operation", ["align", "realign"])
def test_alignment_reports_before_index_building_or_fastq_extraction(
    operation, tmp_path, source, monkeypatch
):
    ref = tmp_path / "ref.fa"
    ref.write_text(">chr1\nA\n")

    def reported(estimate):
        assert estimate.operation == operation
        if operation == "realign":
            assert estimate.resources[1].components["extracted_fastqs"] > 0
        raise Reported

    if operation == "align":
        monkeypatch.setattr(
            align,
            "_prepare_alignment",
            lambda *_: align._AlignSetup(
                "1", "256M", str(tmp_path), str(ref), "sample"
            ),
        )
        monkeypatch.setattr(align, "fastq_input_reads", lambda *_: 100)
        monkeypatch.setattr(align, "report_resources", reported)
        monkeypatch.setattr(
            align, "ensure_bwa_index", lambda *_: pytest.fail("indexing started")
        )
        with pytest.raises(Reported):
            align._bwa_setup(
                argparse.Namespace(r1="r1.fq", r2=None, aligner="bwa"), "align"
            )
    else:
        monkeypatch.setattr(realign, "resolve_reference", lambda *_: str(ref))
        monkeypatch.setattr(realign, "indexed_read_count", lambda *_: 100)
        monkeypatch.setattr(realign, "report_resources", reported)
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
                "illumina",
                "--threads",
                "1",
                "--no-preflight",
            )
