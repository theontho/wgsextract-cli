"""``wgsextract report``: a sample's results as one local report.

Sections follow which runs exist (missing ones say how to run them), findings
keep their confidence labels and always carry evidence and the "technical
result, not diagnosis" disclaimer, the JSON follows its schema, and rendering
is offline and keeps the sample's absolute paths out of the documents."""

import gzip
import hashlib
import io
import json
import os
import shutil
import socket
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jsonschema
import pytest
from markdown_it import MarkdownIt

from wgsextract_cli import api
from wgsextract_cli.core import gene_lists
from wgsextract_cli.core import personal_report as pr
from wgsextract_cli.core.config import get_config_path, reload_settings, settings
from wgsextract_cli.core.sample_workspace import SampleWorkspace
from wgsextract_cli.core.utils import UsageError
from wgsextract_cli.main import main

TSV_HEADER = (
    "# ClinVar pathogenic/likely pathogenic calls: wgsextract confidence labels\n"
    "chrom\tpos\tref\talt\tgenotype\tzygosity\tconfidence\treasons\tdepth\t"
    "genotype_quality\talt_fraction\tCLNSIG\tCLNDN\n"
)


# Three genes in the built-in lists' format, with their NCBI RefSeq spans.
MINI_LIST = (
    "# A test gene list\n"
    "gene\tcategory\thg19_chrom\thg19_start\thg19_end\thg38_chrom\thg38_start\t"
    "hg38_end\tnote\n"
    "BRCA2\tcancer\tchr13\t32889645\t32974405\tchr13\t32315077\t32400268\t\n"
    "MUTYH\tcancer\tchr1\t45794914\t45806112\tchr1\t45329242\t45340440\t"
    "two P/LP variants (biallelic) are reportable\n"
    "PLN\tcardiovascular\tchr6\t118869459\t118882879\tchr6\t118548296\t"
    "118561716\t\n"
)
SPANS = {"BRCA2": ("chr13", 32315077, 32400268), "MUTYH": ("chr1", 45329242, 45340440)}


@dataclass
class ServedLists:
    library: Path
    body: bytes = MINI_LIST.encode()
    online: bool = True
    downloads: list[str] = field(default_factory=list)


@pytest.fixture(autouse=True)
def served_lists(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> ServedLists:
    """Every built-in gene list is MINI_LIST, served by a counted download
    into a fresh reference library."""
    served = ServedLists(tmp_path_factory.mktemp("library"))
    # In config.toml too, which the CLI reloads.
    config = get_config_path()
    config.write_text(
        config.read_text() + f"reference_library = {json.dumps(str(served.library))}\n"
    )
    reload_settings()
    pinned = {
        "genes": {"BRCA2": "cancer", "MUTYH": "cancer", "PLN": "cardiovascular"},
        "sha256": hashlib.sha256(MINI_LIST.encode()).hexdigest(),
    }
    lists = {
        list_id: {
            **meta,
            **pinned,
            "download": f"https://example.invalid/{list_id}.tsv",
        }
        for list_id, meta in gene_lists.MANIFEST["lists"].items()
    }
    monkeypatch.setitem(gene_lists.MANIFEST, "lists", lists)

    def download(url: str, dest: str, **_kwargs) -> bool:
        if not served.online:
            return False
        served.downloads.append(url)
        Path(dest).write_bytes(served.body)
        return True

    # The network is the boundary: serve the list locally.
    monkeypatch.setattr(gene_lists, "download_file", download)
    return served


def _mid(symbol: str) -> tuple[str, int]:
    chrom, start, end = SPANS[symbol]
    return chrom, (start + end) // 2


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
    error: str = "tool crashed",
) -> Path:
    path = folder / f"{name}.manifest.json"
    document = {
        "schema": "wgsextract.result/1",
        "command": command,
        "status": status,
        "exit_code": 0 if status == "ok" else 1,
        "error": None
        if status == "ok"
        else {"type": "WGSExtractError", "message": error},
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


# Genes per category in each ACMG SF version's Table 1 (v3.2: Miller et al.
# 2023, PMID 37347242, https://pmc.ncbi.nlm.nih.gov/articles/PMC10524344/;
# v3.3: Lee et al. 2025, PMID 40568962,
# https://pmc.ncbi.nlm.nih.gov/articles/PMC12318660/).
ACMG_SF_TABLE_1 = {
    "3.2": {"cancer": 28, "cardiovascular": 40, "metabolism": 4, "miscellaneous": 9},
    "3.3": {"cancer": 28, "cardiovascular": 41, "metabolism": 7, "miscellaneous": 8},
}


def test_built_in_lists_match_the_cited_acmg_sf_tables():
    # The pinned manifest as shipped (the served_lists fixture replaces its lists).
    pinned = json.loads(
        Path(gene_lists.__file__)
        .parent.parent.joinpath("assets/report/gene_lists.json")
        .read_text()
    )
    lists = pinned["lists"]
    assert pinned["default"] == "acmg-sf-3.3"
    for meta in lists.values():
        by_category: dict[str, int] = {}
        for category in meta["genes"].values():
            by_category[category] = by_category.get(category, 0) + 1
        assert by_category == ACMG_SF_TABLE_1[meta["version"]]
    v32, v33 = (lists[f"acmg-sf-{v}"]["genes"] for v in ("3.2", "3.3"))
    # v3.3 adds three genes and moves HFE to inborn errors of metabolism.
    assert set(v33) - set(v32) == {"ABCD1", "CYP27A1", "PLN"}
    assert set(v32) < set(v33)
    assert (v32["HFE"], v33["HFE"]) == ("miscellaneous", "metabolism")


def _clinvar_table(folder: Path, *calls: tuple[str, int, str, str]) -> None:
    """Replace the sample's ClinVar calls with (chrom, pos, CLNSIG, GENEINFO)."""
    table = folder / "clinvar_pathogenic.confidence.tsv"
    table.write_text(
        TSV_HEADER.replace("CLNDN\n", "CLNDN\tGENEINFO\n")
        + "".join(
            f"{chrom}\t{pos}\tC\tT\t0/1\thet\thigh\t\t30\t99\t0.5\t{sig}\tx\t{info}\n"
            for chrom, pos, sig, info in calls
        )
    )


def test_built_in_list_is_downloaded_once_and_checked_on_each_use(sample, served_lists):
    first = pr.build_report(sample)["sections"]["clinvar"]
    assert first["gene_list"]["id"] == "acmg-sf-3.3"
    assert first["title"].endswith("in ACMG SF v3.3 genes")
    assert pr.build_report(sample)["sections"]["clinvar"]["counts"]["in_gene_list"] == 2
    assert served_lists.downloads == ["https://example.invalid/acmg-sf-3.3.tsv"]
    (stored,) = served_lists.library.joinpath("report", "gene_lists").glob("*.tsv")
    # A changed copy fails its pinned SHA-256 and is downloaded again.
    stored.write_text(MINI_LIST.replace("MUTYH", "MUTYX"))
    again = pr.build_report(sample)["sections"]["clinvar"]
    assert {f["gene"] for f in again["findings"]} == {"BRCA2", "MUTYH"}
    assert len(served_lists.downloads) == 2


@pytest.mark.parametrize("problem", ["offline", "tampered"])
def test_without_a_verified_list_clinvar_calls_are_not_placed(
    sample, served_lists, problem
):
    if problem == "offline":
        served_lists.online = False
    else:
        served_lists.body = MINI_LIST.replace("BRCA2", "BRCA3").encode()
    code, _ = _cli("report", "--workspace", str(sample))
    assert code == 0
    report = json.loads((sample / "HG002_personal_report.json").read_text())
    clinvar = report["sections"]["clinvar"]
    assert [w["code"] for w in clinvar["warnings"]] == ["gene_list_unavailable"]
    assert "--gene-list" in clinvar["warnings"][0]["message"]
    assert clinvar["findings"] == []
    # Unplaced calls are not claimed to lie outside the list.
    assert clinvar["counts"]["carried"] == 3
    assert clinvar["counts"]["outside_gene_list"] == 0
    assert "not placed in genes" in clinvar["summary"]
    assert report["sections"]["lineage"]["findings"]


def test_a_report_without_clinvar_calls_downloads_no_list(sample, served_lists):
    served_lists.online = False
    _clinvar_table(sample, (*_mid("BRCA2"), "Benign", "BRCA2:675"))
    clinvar = pr.build_report(sample)["sections"]["clinvar"]
    assert clinvar["findings"] == []
    assert clinvar["gene_list"]["genes"] == 3
    assert served_lists.downloads == [] and clinvar["warnings"] == []


def test_geneinfo_names_the_gene_and_spans_are_the_fallback(sample):
    brca2, mutyh = _mid("BRCA2"), _mid("MUTYH")
    _clinvar_table(
        sample,
        (*brca2, "Pathogenic", "ZAR1L:345862"),  # in BRCA2's span, ClinVar: ZAR1L
        ("chr6", 1000, "Pathogenic", "PLN:5350|OTHER:1"),  # outside every span
        (*mutyh, "Likely_pathogenic", "."),  # no GENEINFO: placed by span
    )
    clinvar = pr.build_report(sample)["sections"]["clinvar"]
    assert sorted(
        (f["gene"], f["evidence"]["region"]) for f in clinvar["findings"]
    ) == [
        ("MUTYH", f"chr1:{mutyh[1]}"),
        ("PLN", "chr6:1000"),
    ]
    assert clinvar["counts"]["outside_gene_list"] == 1


def test_gene_list_from_the_flag_or_config_is_a_tsv_or_bed(
    sample, tmp_path, monkeypatch, served_lists
):
    brca2, mutyh = _mid("BRCA2"), _mid("MUTYH")
    # A BED file names no build: its spans are in the VCF's (0-based starts).
    bed = tmp_path / "my_panel.bed"
    bed.write_text(
        f"track name=mine\n{mutyh[0]}\t{mutyh[1] - 1}\t{mutyh[1]}\tMUTYH\n"
        f"{brca2[0]}\t{brca2[1]}\t{brca2[1] + 10}\tBRCA2\n"
    )
    code, _ = _cli("report", "--workspace", str(sample), "--gene-list", str(bed))
    assert code == 0
    report = json.loads((sample / "HG002_personal_report.json").read_text())
    jsonschema.validate(report, pr.load_schema())
    clinvar = report["sections"]["clinvar"]
    assert [f["gene"] for f in clinvar["findings"]] == ["MUTYH"]
    assert clinvar["title"].endswith("in my_panel.bed genes")
    assert clinvar["gene_list"] == {
        "id": "my_panel.bed",
        "name": "my_panel.bed",
        "version": None,
        "citation": None,
        "url": None,
        "genes": 2,
    }
    assert str(tmp_path) not in json.dumps(report)

    # A list of symbols alone relies on ClinVar's GENEINFO.
    symbols = tmp_path / "symbols.tsv"
    symbols.write_text("# mine\ngene\nBRCA2\n")
    monkeypatch.setitem(settings, "report_gene_list", str(symbols))
    _clinvar_table(
        sample,
        (*brca2, "Pathogenic", "BRCA2:675"),
        (*mutyh, "Pathogenic", ""),
    )
    result = api.report.build(api.ReportParams(workspace=sample))
    clinvar = result.details["sections"]["clinvar"]
    assert [f["gene"] for f in clinvar["findings"]] == ["BRCA2"]
    assert clinvar["gene_list"]["id"] == "symbols.tsv"
    assert served_lists.downloads == []


def test_an_unknown_gene_list_is_a_usage_error(sample, tmp_path):
    code, _ = _cli("report", "--workspace", str(sample), "--gene-list", "acmg-sf-9")
    assert code == 2
    bad = tmp_path / "bad.bed"
    bad.write_text("chr1\t10\t20\n")
    with pytest.raises(api.CommandFailed, match="column 4"):
        api.report.build(api.ReportParams(workspace=sample, gene_list=str(bad)))
    assert not list(sample.glob("*_personal_report*"))


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


HOSTILE = '<img src=x onerror="alert(1)">&amp;[x](javascript:alert(1))|`*_'


class _Page(HTMLParser):
    """The tags, attributes, links and visible text of an HTML page."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()
        self.attributes: set[str] = set()
        self.hrefs: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        self.attributes.update(name for name, _ in attrs)
        self.hrefs += [value or "" for name, value in attrs if name == "href"]

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _assert_shown_not_rendered(page_html: str, tags: set[str]) -> None:
    page = _Page()
    page.feed(page_html)
    assert page.tags <= tags
    assert not {a for a in page.attributes if a.startswith("on")}
    assert not [h for h in page.hrefs if h.lower().startswith("javascript:")]
    assert HOSTILE in "".join(page.text)


def _hostile(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _hostile(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_hostile(v) for v in value]
    return HOSTILE if isinstance(value, str) else value


def test_text_from_results_is_shown_as_text_in_html_and_markdown(sample):
    pos = _mid("BRCA2")
    rows = [*pos, *[HOSTILE] * 4, "high", "", 30, 99, 0.5, f"Pathogenic{HOSTILE}"]
    rows.append(HOSTILE)
    table = sample / "clinvar_pathogenic.confidence.tsv"
    table.write_text(TSV_HEADER + "\t".join(map(str, rows)) + "\n")
    haplogrep = json.loads((sample / "haplogrep.manifest.json").read_text())
    haplogrep["metrics"]["haplogroup"] = HOSTILE
    (sample / "haplogrep.manifest.json").write_text(json.dumps(haplogrep))
    report = pr.build_report(sample)
    (clinvar,) = report["sections"]["clinvar"]["findings"]
    assert {clinvar["variant"][k] for k in ("ref", "alt", "genotype", "zygosity")} == {
        HOSTILE
    }
    assert HOSTILE.replace("_", " ") in clinvar["detail"]
    assert report["sections"]["lineage"]["findings"][0]["value"] == HOSTILE

    # Every other string (sample name, folder, files, regions, labels...) too.
    report = _hostile(report)
    html_tags = {"html", "head", "meta", "title", "style", "body", "h1", "h2"}
    html_tags |= {"p", "b", "i", "br", "code", "span", "a", "ul", "li"}
    html_tags |= {"table", "tr", "th", "td"}
    _assert_shown_not_rendered(pr.render_html(report), html_tags)
    markdown = MarkdownIt("commonmark", {"html": True}).enable("table")
    md_tags = {"h1", "h2", "p", "blockquote", "strong", "em", "code", "a", "ul"}
    md_tags |= {"li", "table", "thead", "tbody", "tr", "th", "td"}
    _assert_shown_not_rendered(markdown.render(pr.render_markdown(report)), md_tags)


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


def test_outside_a_workspace_the_overview_falls_back_to_info(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    cram = _touch(folder / "s.cram")
    _manifest(
        folder,
        "s.cram_chromosomes.tsv",
        "info",
        inputs=((cram, "input"),),
        metrics={"sex": "Male", "ard_mapped": 39.03, "sequencer": "NovaSeq"},
    )
    report = pr.build_report(folder)
    facts = {f["name"]: f for f in report["sections"]["overview"]["facts"]}
    assert facts["sex"]["value"] == "Male"
    assert facts["sex"]["source"] == "info" and facts["sex"]["confidence"] is None
    assert facts["sex"]["file"] == "s.cram"
    assert facts["mean_coverage"]["text"] == "39x"
    assert "| Sex | Male |" in pr.render_markdown(report)
    assert "not recorded" in pr.render_markdown(report)
    jsonschema.validate(report, pr.load_schema())


def _two_people(folder: Path) -> Path:
    """Alice's and Bob's runs side by side in a plain folder."""
    folder.mkdir()
    for person, haplogroup in (("Alice", "H1a"), ("Bob", "U5b")):
        cram = _touch(folder / f"{person}.cram")
        _manifest(
            folder,
            f"{person}_haplogrep",
            "lineage mt-haplogroup",
            inputs=((cram, "input"),),
            outputs=((_touch(folder / f"{person}_mt.txt"), "haplogroup_report"),),
            metrics={"haplogroup": haplogroup, "haplogroup_quality": 0.95},
            finished="2025-01-02T00:00:00+00:00" if person == "Bob" else "2025-01-01",
        )
    return folder


def test_an_input_limits_the_report_to_that_samples_runs(tmp_path):
    folder = _two_people(tmp_path / "shared")
    alice = folder / "Alice.cram"
    vcf = _touch(folder / "Alice.snps.vcf.gz")
    _manifest(
        folder,
        "Alice_snps",
        "vcf snp",
        inputs=((alice, "input"),),
        outputs=((vcf, "vcf"),),
    )
    _manifest(  # on a file made from Alice's alignment
        folder,
        "Alice_pharmcat",
        "vcf pharmcat",
        inputs=((vcf, "vcf_input"),),
        status="failed",
    )
    report = pr.build_report(folder, input_file=alice)
    assert report["sample"]["name"] == "Alice"
    (mt,) = report["sections"]["lineage"]["findings"]
    assert mt["value"] == "H1a"
    assert report["sections"]["pharmacogenomics"]["status"] == "failed"
    sources = {s["manifest"] for s in report["sections"]["lineage"]["sources"]}
    assert sources == {"Alice_haplogrep.manifest.json"}

    with pytest.raises(UsageError, match="Alice, Bob"):
        pr.build_report(folder)
    code, out = _cli("report", "--workspace", str(folder))
    assert code == 2


def test_in_a_workspace_runs_on_another_alignment_stay_out(tmp_path):
    folder = _two_people(tmp_path / "genome")
    (folder / "genome-config.toml").write_text("")
    outside_vcf = _touch(folder / "from_a_lab.vcf.gz")  # the same person's
    _manifest(
        folder,
        "lab_clinvar",
        "vcf clinvar",
        inputs=((outside_vcf, "vcf_input"),),
        status="failed",
        error="bcftools failed",
    )
    report = pr.build_report(folder, input_file=folder / "Alice.cram")
    assert [f["value"] for f in pr.findings(report)] == ["H1a"]
    assert report["sections"]["clinvar"]["status"] == "failed"


def test_failed_pharmcat_and_clinvar_runs_show_their_errors(sample):
    vcf = sample / "HG002.vcf.gz"
    for name, command in (("pgx", "vcf pharmcat"), ("cv", "vcf clinvar")):
        _manifest(
            sample,
            name,
            command,
            inputs=((vcf, "vcf_input"),),
            status="failed",
            error=f"{command} needs java",
            finished="2026-01-01T00:00:00+00:00",
        )
    (sample / "clinvar_pathogenic.vcf.manifest.json").unlink()
    sections = pr.build_report(sample)["sections"]
    assert sections["clinvar"]["status"] == "failed"
    assert "vcf clinvar needs java" in sections["clinvar"]["error"]
    pgx = sections["pharmacogenomics"]
    assert pgx["status"] == "ok"  # Cyrius ran
    assert "vcf pharmcat needs java" in pgx["error"]
    assert [m["command"].split()[1:3] for m in pgx["missing"]] == [["vcf", "pharmcat"]]


def test_messages_from_runs_keep_paths_out_of_the_documents(sample, tmp_path):
    table = sample / "clinvar_pathogenic.confidence.tsv"
    elsewhere = tmp_path / "secret-home" / "ref" / "clinvar.vcf.gz"
    path = sample / "clinvar_pathogenic.vcf.manifest.json"
    document = json.loads(path.read_text())
    document["warnings"] = [
        {
            "code": "clinical_findings",
            "message": f"1 ClinVar call is low confidence; see {table}",
        }
    ]
    path.write_text(json.dumps(document))
    _manifest(
        sample,
        "HG002_microarray",
        "microarray",
        inputs=((sample / "HG002.cram", "input"),),
        status="failed",
        error=f"cannot read {elsewhere}: no such file",
    )
    report = pr.build_report(sample)
    clinvar = report["sections"]["clinvar"]
    assert clinvar["warnings"][0]["message"].endswith(
        "see clinvar_pathogenic.confidence.tsv"
    )
    assert "cannot read clinvar.vcf.gz" in report["sections"]["microarray"]["error"]
    for text in (
        pr.render_html(report),
        pr.render_markdown(report),
        json.dumps(report),
    ):
        assert str(tmp_path) not in text and "secret-home" not in text


def test_a_build_comes_from_the_evidence_vcf_not_stale_facts(sample):
    # the workspace thinks the input VCF is hg19; the result's header says hg38
    SampleWorkspace(sample).record(
        "build", sample / "HG002.vcf.gz", "hg19", method="old", confidence="high"
    )
    with gzip.open(sample / "clinvar_pathogenic.vcf.gz", "wt") as stream:
        stream.write("##fileformat=VCFv4.2\n##contig=<ID=chr1,length=248956422>\n")
        stream.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
    clinvar = pr.build_report(sample)["sections"]["clinvar"]
    assert clinvar["build"] == "hg38"
    assert {f["gene"] for f in clinvar["findings"]} == {"BRCA2", "MUTYH"}


def test_with_only_a_vcf_the_overrides_still_apply(tmp_path):
    folder = tmp_path / "vcf-only"
    folder.mkdir()
    (folder / "genome-config.toml").write_text("")
    vcf = _touch(folder / "me.vcf.gz")
    _manifest(folder, "me_clinvar", "vcf clinvar", inputs=((vcf, "vcf_input"),))
    SampleWorkspace(folder).set_overrides({"sex": "Female"})
    report = pr.build_report(folder)
    facts = {f["name"]: f for f in report["sections"]["overview"]["facts"]}
    assert facts["sex"]["value"] == "Female"
    assert facts["sex"]["source"] == "override"
    lineage = report["sections"]["lineage"]
    assert [m["what"] for m in lineage["missing"]] == ["mtDNA haplogroup"]


def test_a_copied_folder_reports_on_its_own_evidence(sample, tmp_path):
    copy = Path(shutil.copytree(sample, tmp_path / "copy"))
    table = copy / "clinvar_pathogenic.confidence.tsv"
    table.write_text(TSV_HEADER)  # the copy has no calls; the original has 4
    report = pr.build_report(copy)
    clinvar = report["sections"]["clinvar"]
    assert clinvar["counts"]["carried"] == 0
    assert clinvar["all_calls"] == "clinvar_pathogenic.confidence.tsv"
    (mt,) = report["sections"]["lineage"]["findings"]
    assert mt["evidence"]["file"] == "haplogrep_results.txt"
    warnings = [w["code"] for s in report["sections"].values() for w in s["warnings"]]
    assert "evidence_missing" not in warnings


def test_results_on_another_drive_keep_their_location(sample, tmp_path, monkeypatch):
    elsewhere = tmp_path / "D"
    elsewhere.mkdir()
    kit = _touch(elsewhere / "HG002_CombinedKit.txt")
    _manifest(
        elsewhere,
        "kit",
        "microarray",
        inputs=((sample / "HG002.cram", "input"),),
        outputs=((kit, "combined_kit"),),
    )
    relpath = os.path.relpath

    def across_drives(path: str, start: str = os.curdir) -> str:
        if str(path).startswith(str(elsewhere)):
            raise ValueError("path is on mount 'D:', start on mount 'C:'")
        return relpath(path, start)

    monkeypatch.setattr(pr.os.path, "relpath", across_drives)
    report = pr.build_report(sample, results=[elsewhere])
    (file,) = report["sections"]["microarray"]["files"]
    assert Path(file["file"]) == kit
    assert f'href="{kit.as_uri()}"' in pr.render_html(report)


def test_report_from_an_alignment_needs_no_samtools(sample, monkeypatch):
    def no_samtools(path: Any) -> None:
        raise AssertionError("report checked the alignment with samtools")

    monkeypatch.setattr(
        "wgsextract_cli.core.alignment_input.validate_alignment_input", no_samtools
    )
    cram = sample / "HG002.cram"
    code, out = _cli("report", "-i", str(cram), "--outdir", str(sample / "out"))
    assert code == 0, out
    result = api.report.build(api.ReportParams(input=cram, outdir=sample / "out2"))
    assert result.exit_code == 0
