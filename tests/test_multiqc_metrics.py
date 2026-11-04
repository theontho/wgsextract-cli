"""MultiQC custom content from result manifests (#354)."""

import json
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools, run_wgsextract
from wgsextract_cli.core import multiqc
from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.utils import WGSExtractError


def _manifest(
    path: Path,
    command: str,
    input_path: str,
    finished: str = "2025-01-01T00:00:00Z",
    status: str = "ok",
    role: str = "input",
    outputs: list[tuple[str, str, dict]] | None = None,
    warnings: tuple[str, ...] = (),
    **metrics,
) -> Path:
    result = CommandResult(command=command, status=status)  # type: ignore[arg-type]
    result.add_input(input_path, role)
    for output, output_role, output_metrics in outputs or []:
        result.add_output(output, output_role, metrics=output_metrics)
    result.add_metrics(**metrics)
    for code in warnings:
        result.warn(code, code)
    document = result.to_dict()
    document["provenance"]["finished"] = finished
    path.write_text(json.dumps(document))
    return path


def _rows(files: dict, section: str) -> dict:
    return files[f"wgsextract_{section}_mqc.json"]["data"]


def test_sections_rows_and_general_stats(tmp_path):
    _manifest(
        tmp_path / "a.info.json",
        "info",
        "/data/NA12878.cram",
        sex="Female",
        ard_mapped=31.2,
        reads_mapped_pct=99.1,
        warnings=("low_coverage",),
    )
    _manifest(
        tmp_path / "haplogrep_results.txt.manifest.json",
        "lineage mt-haplogroup",
        "/data/NA12878.cram",
        haplogroup="H2a2a1",
        haplogroup_quality=0.93,
    )
    _manifest(
        tmp_path / "snps.vcf.manifest.json",
        "vcf snp",
        "/data/NA12878.cram",
        variants=4_100_000,
    )
    _manifest(
        tmp_path / "k.manifest.json",
        "microarray",
        "/data/joint.vcf.gz",
        outputs=[
            ("k1.txt", "vendor_kit", {"no_call_rate": 0.01}),
            ("k2.txt", "vendor_kit", {"no_call_rate": 0.02}),
        ],
        sample="NA12878",
        autosomal_called=600_000,
        autosomal_homozygosity=0.65,
    )
    files = multiqc.custom_content(d for _p, d in multiqc.find_documents(tmp_path))

    assert set(files) == {
        f"wgsextract_{s}_mqc.json"
        for s in (
            "alignment",
            "lineage",
            "microarray",
            "variants",
            "warnings",
            "general_stats",
        )
    }
    assert _rows(files, "alignment") == {
        "NA12878": {"sex": "Female", "ard_mapped": 31.2, "reads_mapped_pct": 99.1}
    }
    # The microarray of a joint VCF is named after the sample it selected.
    assert _rows(files, "microarray") == {
        "NA12878": {
            "autosomal_called": 600_000,
            "autosomal_homozygosity": 65.0,
            "kits": 2,
            "max_no_call_pct": 2.0,
        }
    }
    assert _rows(files, "lineage")["NA12878"]["mt_haplogroup"] == "H2a2a1"
    assert _rows(files, "variants") == {"NA12878": {"vcf_snp": 4_100_000}}
    assert _rows(files, "warnings") == {
        "NA12878": {"warnings": 1, "codes": "low_coverage"}
    }
    general = files["wgsextract_general_stats_mqc.json"]
    assert general["plot_type"] == "generalstats"
    assert general["data"]["NA12878"]["wgsextract_mt_haplogroup"] == "H2a2a1"
    table = files["wgsextract_alignment_mqc.json"]
    assert table["parent_id"] == "wgsextract" and table["plot_type"] == "table"
    assert set(table["headers"]) == {"sex", "ard_mapped", "reads_mapped_pct"}


def test_latest_successful_run_wins_and_failures_are_skipped(tmp_path):
    for name, finished, status, depth in (
        ("new", "2025-02-01T00:00:00Z", "ok", 30.0),
        ("old", "2025-01-01T00:00:00Z", "ok", 10.0),
        ("failed", "2025-03-01T00:00:00Z", "failed", 1.0),
    ):
        _manifest(
            tmp_path / f"{name}.json",
            "info",
            "s1.bam",
            finished=finished,
            status=status,
            ard_mapped=depth,
        )
    files = multiqc.custom_content(d for _p, d in multiqc.find_documents(tmp_path))
    assert _rows(files, "alignment") == {"s1": {"ard_mapped": 30.0}}


@pytest.mark.parametrize(
    ("path", "role", "sample"),
    [
        ("/x/HG002.cram", "input", "HG002"),
        ("/x/P1_N.deepvariant.g.vcf.gz", "vcf_input", "P1_N.deepvariant"),
        ("/x/P1_R1.fastq.gz", "fastq_r1", "P1_R1"),
    ],
)
def test_sample_name_strips_type_extensions(path, role, sample):
    document = {"inputs": [{"path": path, "role": role}]}
    assert multiqc.sample_name(document) == sample


def test_discovery_skips_other_json_hidden_dirs_and_own_files(tmp_path):
    _manifest(tmp_path / "info.json", "info", "s.bam", ard_mapped=1.0)
    (tmp_path / "other.json").write_text('{"schema": "something/1"}')
    (tmp_path / "broken.manifest.json").write_text("{")
    hidden = tmp_path / ".cache"
    hidden.mkdir()
    _manifest(hidden / "x.manifest.json", "info", "h.bam", ard_mapped=1.0)
    (tmp_path / "wgsextract_alignment_mqc.json").write_text("{}")
    assert [p.name for p, _d in multiqc.find_documents(tmp_path)] == ["info.json"]


def test_write_removes_stale_section_files(tmp_path):
    (tmp_path / "wgsextract_lineage_mqc.json").write_text("{}")
    (tmp_path / "unrelated_mqc.json").write_text("{}")
    files = {"wgsextract_alignment_mqc.json": {"id": "x"}}
    written = multiqc.write_custom_content(files, tmp_path)
    assert written == [tmp_path / "wgsextract_alignment_mqc.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "unrelated_mqc.json",
        "wgsextract_alignment_mqc.json",
    ]


def test_multiqc_metrics_command(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    with pytest.raises(WGSExtractError, match="No WGS Extract result manifests"):
        run_wgsextract("qc", "multiqc-metrics", "--input", str(runs))
    _manifest(runs / "x.manifest.json", "extract mito-bam", "s.bam")
    with pytest.raises(WGSExtractError, match="has metrics MultiQC can show"):
        run_wgsextract("qc", "multiqc-metrics", "--input", str(runs))
    _manifest(runs / "info.json", "info", "s.bam", ard_mapped=12.5)
    out = tmp_path / "mqc"
    run_wgsextract("qc", "multiqc-metrics", "--input", str(runs), "--outdir", str(out))
    alignment = json.loads((out / "wgsextract_alignment_mqc.json").read_text())
    assert alignment["data"] == {"s": {"ard_mapped": 12.5}}


@needs_tools("multiqc")
def test_multiqc_report_shows_wgsextract_sections(tmp_path):
    _manifest(tmp_path / "info.json", "info", "HG002.cram", ard_mapped=33.3)
    _manifest(
        tmp_path / "mt.manifest.json",
        "lineage mt-haplogroup",
        "HG002.cram",
        haplogroup="U5a1a1",
    )
    out = tmp_path / "report"
    run_wgsextract("qc", "multiqc", "--input", str(tmp_path), "--outdir", str(out))
    html = (out / "multiqc_report.html").read_text()
    assert "WGS Extract" in html and "Haplogroups" in html and "U5a1a1" in html
    assert sorted(p.name for p in out.iterdir()) == ["multiqc_report.html"]
