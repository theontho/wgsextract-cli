"""``wgsextract report``: a sample's results as one local report.

Sections follow which runs exist (missing ones say how to run them), findings
keep their confidence labels and always carry evidence and the "technical
result, not diagnosis" disclaimer, the JSON follows its schema, and rendering
is offline and keeps the sample's absolute paths out of the documents."""

import io
import json
import shutil
import socket
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jsonschema
import pytest

from wgsextract_cli import api
from wgsextract_cli.core import personal_report as pr
from wgsextract_cli.core.sample_workspace import SampleWorkspace
from wgsextract_cli.main import main

TSV_HEADER = (
    "# ClinVar pathogenic/likely pathogenic calls: wgsextract confidence labels\n"
    "chrom\tpos\tref\talt\tgenotype\tzygosity\tconfidence\treasons\tdepth\t"
    "genotype_quality\talt_fraction\tCLNSIG\tCLNDN\n"
)


def _gene(symbol: str) -> pr.Gene:
    return next(g for g in pr.gene_list() if g.symbol == symbol)


def _mid(symbol: str) -> tuple[str, int]:
    chrom, start, end = _gene(symbol).spans["hg38"]
    return f"chr{chrom}", (start + end) // 2


def _touch(path: Path, text: str = "x") -> Path:
    path.write_text(text)
    return path


def _manifest(
    folder: Path,
    name: str,
    command: str,
    *,
    inputs: tuple[tuple[Path, str], ...] = (),
    outputs: tuple[tuple[Path, str], ...] = (),
    metrics: dict[str, Any] | None = None,
    warnings: tuple[tuple[str, str], ...] = (),
    status: str = "ok",
    finished: str = "2025-01-01T00:00:00+00:00",
) -> Path:
    path = folder / f"{name}.manifest.json"
    document = {
        "schema": "wgsextract.result/1",
        "command": command,
        "status": status,
        "exit_code": 0 if status == "ok" else 1,
        "error": None
        if status == "ok"
        else {"type": "WGSExtractError", "message": "tool crashed"},
        "inputs": [{"path": str(p), "role": r} for p, r in inputs],
        "outputs": [{"path": str(p), "role": r, "metrics": {}} for p, r in outputs],
        "metrics": metrics or {},
        "warnings": [{"code": c, "message": m, "details": {}} for c, m in warnings],
        "details": None,
        "manifest": str(path),
        "provenance": {"started": finished, "finished": finished},
    }
    path.write_text(json.dumps(document))
    return path


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    """A sample folder after info-less runs: mtDNA lineage, ClinVar and Cyrius."""
    folder = tmp_path / "HG002"
    folder.mkdir()
    (folder / "genome-config.toml").write_text("")  # a genome-library folder
    cram = _touch(folder / "HG002.cram")
    vcf = _touch(folder / "HG002.vcf.gz")
    workspace = SampleWorkspace(folder)
    workspace.record("sex", cram, "Male", method="chrY reads", confidence="high")
    workspace.record("build", vcf, "hg38", method="VCF header", confidence="high")

    haplogrep = _touch(folder / "haplogrep_results.txt")
    _manifest(
        folder,
        "haplogrep",
        "lineage mt-haplogroup",
        inputs=((cram, "input"),),
        outputs=((haplogrep, "haplogroup_report"),),
        metrics={"haplogroup": "H1a", "haplogroup_quality": 0.95},
    )

    brca2, mutyh = _mid("BRCA2"), _mid("MUTYH")
    rows = [
        (*brca2, "C", "T", "0/1", "het", "high", "", 34, 99, 0.5, "Pathogenic", "HBOC"),
        (
            *mutyh,
            "G",
            "A",
            "0/1",
            "het",
            "low",
            "low_depth|low_gq",
            6,
            12,
            0.33,
            "Likely_pathogenic",
            "MAP",
        ),  # fmt: skip
        (
            "chr1",
            1000,
            "A",
            "G",
            "0/1",
            "het",
            "moderate",
            "moderate_depth",
            15,
            40,
            0.4,
            "Pathogenic",
            "Elsewhere",
        ),  # fmt: skip
        (
            *brca2,
            "C",
            "G",
            "0/1",
            "het",
            "high",
            "",
            30,
            99,
            0.5,
            "Conflicting_classifications_of_pathogenicity",
            "x",
        ),  # fmt: skip
    ]
    table = folder / "clinvar_pathogenic.confidence.tsv"
    table.write_text(TSV_HEADER + "".join("\t".join(map(str, r)) + "\n" for r in rows))
    _manifest(
        folder,
        "clinvar_pathogenic.vcf",
        "vcf clinvar",
        inputs=((vcf, "input"),),
        outputs=(
            (_touch(folder / "clinvar_pathogenic.vcf.gz"), "clinvar_pathogenic"),
            (table, "confidence_table"),
        ),
    )

    calls = _touch(folder / "HG002_cyp2d6.tsv")
    _manifest(
        folder,
        "HG002_cyp2d6",
        "typing cyp2d6",
        inputs=((cram, "input"),),
        outputs=((calls, "cyp2d6_calls"),),
        metrics={
            "cyp2d6_genotype": "*1/*2",
            "cyp2d6_filter": "PASS",
            "median_depth": 31.0,
        },
    )
    return folder


def _cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = 0
    with redirect_stdout(out), patch.object(sys, "argv", ["wgsextract", *argv]):
        try:
            main()
        except SystemExit as e:
            code = int(e.code or 0)
    return code, out.getvalue()


def test_sections_follow_the_runs_and_missing_ones_say_how_to_run(sample):
    report = pr.build_report(sample)
    sections = report["sections"]
    assert {name: s["status"] for name, s in sections.items()} == {
        "overview": "ok",  # workspace facts, though info has not run
        "lineage": "ok",
        "pharmacogenomics": "ok",
        "clinvar": "ok",
        "microarray": "not_run",
    }
    hints = {n: [m["command"] for m in s["missing"]] for n, s in sections.items()}
    assert hints["overview"] == ["wgsextract info --detailed -i HG002.cram"]
    assert hints["lineage"] == ["wgsextract lineage y-haplogroup -i HG002.cram"]
    assert hints["pharmacogenomics"] == [
        "wgsextract vcf pharmcat --vcf-input HG002.vcf.gz"
    ]
    assert hints["microarray"] == ["wgsextract microarray -i HG002.cram"]
    assert hints["clinvar"] == []
    sex = next(f for f in sections["overview"]["facts"] if f["name"] == "sex")
    assert (sex["value"], sex["method"], sex["confidence"]) == (
        "Male",
        "chrY reads",
        "high",
    )
    assert report["sample"]["name"] == "HG002"


def test_y_dna_is_not_asked_for_a_female_sample(sample):
    SampleWorkspace(sample).set_overrides({"sex": "Female"}, None)
    lineage = pr.build_report(sample)["sections"]["lineage"]
    assert lineage["missing"] == []
    assert "recorded sex Female" in lineage["summary"]


def test_clinvar_lists_gene_list_hits_with_their_call_confidence(sample):
    clinvar = pr.build_report(sample)["sections"]["clinvar"]
    hits = {f["gene"]: f for f in clinvar["findings"]}
    assert set(hits) == {"BRCA2", "MUTYH"}
    assert hits["BRCA2"]["confidence"] == "high"
    assert hits["MUTYH"]["confidence"] == "low"
    assert hits["MUTYH"]["confidence_reasons"] == ["low_depth", "low_gq"]
    assert "biallelic" in hits["MUTYH"]["note"]
    assert hits["MUTYH"]["variant"]["depth"] == 6
    chrom, pos = _mid("MUTYH")
    assert hits["MUTYH"]["evidence"] == {
        "file": "clinvar_pathogenic.vcf.gz",
        "region": f"{chrom}:{pos}",
        "manifest": "clinvar_pathogenic.vcf.manifest.json",
    }
    # Conflicting classifications are not P/LP; chr1:1000 is outside the list.
    assert clinvar["counts"] == {
        "carried": 3,
        "in_gene_list": 2,
        "outside_gene_list": 1,
        "high": 1,
        "moderate": 0,
        "low": 1,
    }
    assert clinvar["all_calls"] == "clinvar_pathogenic.confidence.tsv"


def test_pgx_and_lineage_labels_come_from_the_callers_evidence(sample):
    sections = pr.build_report(sample)["sections"]
    (cyp2d6,) = sections["pharmacogenomics"]["findings"]
    assert (cyp2d6["value"], cyp2d6["confidence"]) == ("*1/*2", "high")
    (mt,) = sections["lineage"]["findings"]
    assert (mt["value"], mt["confidence"]) == ("H1a", "high")
    assert any("H1a" in link["url"] for link in mt["links"])


def test_pharmcat_genes_take_their_weakest_labelled_call(sample):
    def diplotype(a: str, b: str, phenotype: str) -> dict[str, Any]:
        return {
            "recommendationDiplotypes": [
                {
                    "allele1": {"name": a},
                    "allele2": {"name": b},
                    "phenotypes": [phenotype],
                }  # fmt: skip
            ]
        }

    phenotypes = sample / "HG002.phenotype.json"
    phenotypes.write_text(
        json.dumps(
            {
                "geneReports": {
                    "CYP2C19": diplotype("*1", "*17", "Rapid Metabolizer"),
                    "SLCO1B1": diplotype("*1", "*1", "Normal Function"),
                }
            }
        )
    )
    table = sample / "HG002.confidence.tsv"
    table.write_text(
        "# PharmCAT calls\nchrom\tpos\tref\talt\tgenotype\tzygosity\tconfidence\t"
        "reasons\tdepth\tgenotype_quality\talt_fraction\tgene\n"
        "chr10\t1\tC\tT\t0/1\thet\thigh\t\t30\t99\t0.5\tCYP2C19\n"
        "chr10\t2\tG\tA\t0/1\thet\tmoderate\tmoderate_gq\t30\t25\t0.5\tCYP2C19\n"
    )
    report = _touch(sample / "HG002.report.html")
    _manifest(
        sample,
        "HG002.report.html",
        "vcf pharmcat",
        inputs=((sample / "HG002.vcf.gz", "input"),),
        outputs=(
            (report, "pharmcat_report"),
            (phenotypes, "pharmcat_phenotypes"),
            (table, "confidence_table"),
        ),
    )
    findings = pr.build_report(sample)["sections"]["pharmacogenomics"]["findings"]
    genes = {f["gene"]: f for f in findings}
    assert genes["CYP2C19"]["value"] == "*1/*17"
    assert genes["CYP2C19"]["confidence"] == "moderate"
    assert genes["CYP2C19"]["confidence_reasons"] == ["moderate_gq"]
    assert genes["SLCO1B1"]["confidence"] == "not_assessed"
    assert genes["CYP2C19"]["evidence"]["file"] == "HG002.report.html"
    assert genes["CYP2D6"]["confidence"] == "high"  # Cyrius


def test_low_coverage_warning_lowers_a_haplogroup_label(sample):
    _manifest(
        sample,
        "haplogrep",
        "lineage mt-haplogroup",
        outputs=((sample / "haplogrep_results.txt", "haplogroup_report"),),
        metrics={"haplogroup": "H1a", "haplogroup_quality": 0.99},
        warnings=(("low_coverage", "Low coverage."),),
    )
    (mt,) = pr.build_report(sample)["sections"]["lineage"]["findings"]
    assert (mt["confidence"], mt["confidence_reasons"]) == ("low", ["low_coverage"])


def test_a_failed_run_marks_its_section_failed(sample):
    _manifest(
        sample,
        "HG002_microarray",
        "microarray",
        inputs=((sample / "HG002.cram", "input"),),
        outputs=((sample / "HG002_CombinedKit.txt", "combined_kit"),),
        status="failed",
    )
    microarray = pr.build_report(sample)["sections"]["microarray"]
    assert microarray["status"] == "failed"
    assert "tool crashed" in microarray["error"]
    assert microarray["missing"][0]["command"].startswith("wgsextract microarray")


def test_every_finding_carries_evidence_and_the_disclaimer_and_json_is_valid(
    sample,
):
    report = pr.build_report(sample)
    jsonschema.Draft202012Validator.check_schema(pr.load_schema())
    jsonschema.validate(json.loads(json.dumps(report)), pr.load_schema())
    findings = pr.findings(report)
    assert len(findings) == 4
    for finding in findings:
        assert finding["disclaimer"] == "Technical result, not diagnosis."
        assert finding["confidence"] in pr.CONFIDENCE
        assert finding["evidence"]["region"]
        assert (sample / finding["evidence"]["file"]).is_file()
        assert (sample / finding["evidence"]["manifest"]).is_file()
    html = pr.render_html(report)
    markdown = pr.render_markdown(report)
    # One disclaimer per finding plus the banner at the top.
    assert html.count("Technical result, not diagnosis.") == len(findings) + 1
    assert markdown.count("Technical result, not diagnosis.") == len(findings) + 1


def test_a_moved_sample_folder_still_finds_its_evidence(sample, tmp_path):
    moved = Path(shutil.move(sample, tmp_path / "copied" / "HG002-renamed"))
    report = pr.build_report(moved)
    assert len(pr.findings(report)) == 4
    for finding in pr.findings(report):
        assert (moved / finding["evidence"]["file"]).is_file()
    warnings = [w["code"] for s in report["sections"].values() for w in s["warnings"]]
    assert "evidence_missing" not in warnings


def test_results_folders_contribute_only_this_samples_runs(sample, tmp_path):
    elsewhere = tmp_path / "results"
    elsewhere.mkdir()
    other = _touch(tmp_path / "other.cram")
    kit = _touch(elsewhere / "HG002_CombinedKit.txt")
    _manifest(
        elsewhere,
        "mine",
        "microarray",
        inputs=((sample / "HG002.cram", "input"),),
        outputs=((kit, "combined_kit"),),
    )
    _manifest(
        elsewhere,
        "theirs",
        "info",
        inputs=((other, "input"),),
        outputs=((_touch(elsewhere / "other.tsv"), "chromosome_table"),),
        metrics={"ard_mapped": 31.0},
    )
    report = pr.build_report(sample, results=[elsewhere])
    microarray = report["sections"]["microarray"]
    assert microarray["status"] == "ok"
    assert microarray["files"][0]["file"] == "../results/HG002_CombinedKit.txt"
    assert report["sections"]["overview"]["qc"] == []


def test_cli_writes_offline_self_contained_documents(sample, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("report made a network call")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    code, out = _cli("report", "--workspace", str(sample))
    assert code == 0, out
    assert "Technical result, not diagnosis." in out

    html = (sample / "HG002_personal_report.html").read_text()
    markdown = (sample / "HG002_personal_report.md").read_text()
    document = json.loads((sample / "HG002_personal_report.json").read_text())
    jsonschema.validate(document, pr.load_schema())
    for remote in ("<script", "<link", "src=", "@import", "url("):
        assert remote not in html
    assert 'href="clinvar_pathogenic.vcf.gz"' in html
    for text in (html, markdown, json.dumps(document)):
        assert str(sample) not in text

    rows = (sample / "HG002_personal_report_findings.tsv").read_text().splitlines()
    rows = [r for r in rows if not r.startswith("#")]
    assert len(rows) == 1 + len(pr.findings(document))

    manifest = json.loads((sample / "HG002_personal_report.manifest.json").read_text())
    assert manifest["status"] == "ok"
    assert {o["role"] for o in manifest["outputs"]} == {
        "report_html",
        "report_markdown",
        "report_json",
        "findings_table",
    }
    assert manifest["metrics"]["findings_low"] == 1
    assert manifest["metrics"]["section_microarray"] == "not_run"


def test_api_report_to_another_folder_links_back(sample, tmp_path):
    outdir = tmp_path / "reports"
    result = api.report.build(api.ReportParams(workspace=sample, outdir=outdir))
    assert result.details["schema"] == "wgsextract.report/1"
    html = (outdir / "HG002_personal_report.html").read_text()
    assert 'href="../HG002/clinvar_pathogenic.vcf.gz"' in html
    # A second report does not read the first one as a source.
    again = api.report.build(api.ReportParams(workspace=sample, outdir=outdir))
    assert len(pr.findings(again.details)) == len(pr.findings(result.details))


def test_report_needs_a_sample():
    with pytest.raises(api.UsageError):
        api.ReportParams()
