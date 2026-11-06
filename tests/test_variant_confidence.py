"""Confidence labels on ClinVar, rare-variant and discovery findings (#337),
checked through the commands' outputs: the labelled VCF, the TSV and the
result manifest."""

import csv
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.htslib_helpers import needs_tools
from wgsextract_cli.cli.dispatch import dispatch_command
from wgsextract_cli.cli.parser import build_parser, parse_args
from wgsextract_cli.commands._analyze_workflows import run_discovery_filter
from wgsextract_cli.core import results
from wgsextract_cli.core.utils import UsageError, run_command
from wgsextract_cli.core.variant_confidence import Thresholds, label_records

needs_bcftools = needs_tools("bcftools", "bgzip", "tabix")
HG38_CHR1 = 248956422

FORMATS = {
    "GT": "String",
    "AD": "Integer",
    "DP": "Integer",
    "GQ": "Integer",
    "PL": "Integer",
    "VAF": "Float",
}
INFOS = {
    "CLNSIG": "String",
    "CLNDN": "String",
    "GNOMAD_AF": "Float",
    "DP4": "Integer",
    "MQ": "Float",
    "QD": "Float",
}


def _text(
    rows: list[str], *, header: str = "", contig: str = "chr1", mq: str = "MQ"
) -> str:
    """One-sample VCF text; each row is space-separated from POS onwards."""
    return (
        "##fileformat=VCFv4.2\n"
        + header
        + f"##contig=<ID={contig},length={HG38_CHR1}>\n"
        + "".join(
            f'##FILTER=<ID={name},Description="{name}">\n'
            for name in ("RefCall", "LowQual")
        )
        + "".join(
            f'##INFO=<ID={tag},Number=.,Type={kind},Description="{mq if tag == "MQ" else tag}">\n'
            for tag, kind in INFOS.items()
        )
        + "".join(
            f'##FORMAT=<ID={tag},Number=.,Type={kind},Description="{tag}">\n'
            for tag, kind in FORMATS.items()
        )
        + "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        + "".join(f"{contig}\t" + "\t".join(r.split()) + "\n" for r in rows)
    )


def _vcf(path: Path, rows: list[str], **kwargs: str) -> str:
    plain = path.with_suffix("")
    plain.write_text(_text(rows, **kwargs))
    run_command(["bgzip", "-f", str(plain)])
    run_command(["tabix", "-f", "-p", "vcf", str(path)])
    return str(path)


def _labels(vcf: str | Path) -> list[str]:
    out = run_command(
        ["bcftools", "query", "-f", "%POS:%WGSE_CONF:%WGSE_CONF_REASONS\n", str(vcf)],
        capture_output=True,
    )
    return out.stdout.splitlines()


def _table(path: Path) -> list[dict[str, str]]:
    lines = path.read_text().splitlines()
    assert any("not whether a variant causes disease" in ln for ln in lines[:4])
    return list(
        csv.DictReader((ln for ln in lines if not ln.startswith("#")), delimiter="\t")
    )


def _run(tmp_path: Path, *argv: str) -> dict:
    """Run a command as `wgsextract` does and return its result manifest."""
    args, _ = parse_args(*build_parser(), list(argv))
    try:
        dispatch_command(args, set())
    except SystemExit as error:
        assert not error.code
    manifest = next(Path(args.outdir).glob("*.manifest.json"))
    document = json.loads(manifest.read_text())
    errors = list(Draft202012Validator(results.load_schema()).iter_errors(document))
    assert not errors, errors
    return document


# Sample calls at ClinVar pathogenic sites, by their read evidence.
CALLS = [
    "100 good C T 50 PASS . GT:AD:DP:GQ 0/1:15,15:30:99",
    "200 shallow A G 50 PASS . GT:AD:DP:GQ 0/1:3,3:6:40",
    "300 skewed G A 50 PASS . GT:AD:DP:GQ 0/1:27,3:30:50",
    "400 middling T C 50 PASS . GT:AD:DP:GQ 0/1:7,7:14:25",
    "500 refcall C A 0 RefCall . GT:AD:DP:GQ 0/0:30,0:30:50",
    "600 failed A T 50 LowQual . GT:AD:DP:GQ 1/1:0,30:30:60",
]


def _clinvar(tmp_path: Path) -> str:
    sites = [line.split()[:4] for line in CALLS]
    return _vcf(
        tmp_path / "clinvar.vcf.gz",
        [
            f"{pos} {i} {ref} {alt} . . CLNSIG=Pathogenic;CLNDN=Disease_{i} GT 0/1"
            for i, (pos, _, ref, alt) in enumerate(sites)
        ],
    )


@needs_bcftools
def test_clinvar_findings_carry_labels_reasons_and_a_disclaimer(tmp_path):
    sample = _vcf(tmp_path / "s.vcf.gz", CALLS)
    out = tmp_path / "out"
    document = _run(tmp_path, "vcf", "clinvar", "--vcf-input", sample,
                    "--clinvar-file", _clinvar(tmp_path), "--outdir", str(out))  # fmt: skip

    assert _labels(out / "clinvar_pathogenic.vcf.gz") == [
        "100:high:.",
        "200:low:low_depth",
        "300:low:allele_imbalance",
        "400:moderate:moderate_depth|moderate_gq",
        "500:.:.",  # not carried: no label, even though ClinVar lists the site
        "600:low:filtered",
    ]
    rows = _table(out / "clinvar_pathogenic.confidence.tsv")
    assert [(r["pos"], r["confidence"], r["CLNDN"]) for r in rows] == [
        ("100", "high", "Disease_0"),
        ("200", "low", "Disease_1"),
        ("300", "low", "Disease_2"),
        ("400", "moderate", "Disease_3"),
        ("600", "low", "Disease_5"),
    ]
    assert rows[2]["alt_fraction"] == "0.100" and rows[1]["depth"] == "6"

    metrics = document["metrics"]
    assert (metrics["confidence_high"], metrics["confidence_moderate"],
            metrics["confidence_low"], metrics["confidence_not_carried"]) == (1, 1, 3, 1)  # fmt: skip
    warnings = {w["code"]: w for w in document["warnings"]}
    assert warnings["low_confidence_findings"]["details"]["count"] == 3
    assert warnings["unconfirmed_clinical_findings"]["details"]["count"] == 5
    roles = {Path(o["path"]).name: o["role"] for o in document["outputs"]}
    assert roles["clinvar_pathogenic.vcf.gz"] == "clinvar_pathogenic"
    assert roles["clinvar_pathogenic.confidence.tsv"] == "confidence_table"
    (confidence,) = document["details"]["confidence"]
    assert confidence["scheme"] == "wgsextract.confidence/1"
    assert confidence["thresholds"]["min_depth"] == 10
    assert [(f["pos"], f["confidence"], f["reasons"]) for f in confidence["findings"]][
        1
    ] == (200, "low", ["low_depth"])
    assert "accredited clinical laboratory" in confidence["disclaimer"]


@needs_bcftools
@pytest.mark.parametrize("clinvar_has_geneinfo", [True, False])
def test_clinvar_carries_its_geneinfo_into_the_calls_table(
    tmp_path, clinvar_has_geneinfo
):
    # #397: the report names a call's gene from ClinVar's GENEINFO, never from
    # a GENEINFO the input VCF already had.
    geneinfo = '##INFO=<ID=GENEINFO,Number=1,Type=String,Description="Genes">\n'
    sample = _vcf(
        tmp_path / "s.vcf.gz",
        [CALLS[0].replace(" . GT", " GENEINFO=STALE:1 GT")],
        header=geneinfo,
    )
    info = "CLNSIG=Pathogenic;CLNDN=D"
    if clinvar_has_geneinfo:
        info += ";GENEINFO=BRCA2:675|ZAR1L:345862"
    clinvar = _vcf(
        tmp_path / "clinvar.vcf.gz",
        [f"100 0 C T . . {info} GT 0/1"],
        header=geneinfo if clinvar_has_geneinfo else "",
    )
    out = tmp_path / "out"
    _run(tmp_path, "vcf", "clinvar", "--vcf-input", sample, "--clinvar-file",
         clinvar, "--outdir", str(out))  # fmt: skip
    rows = _table(out / "clinvar_pathogenic.confidence.tsv")
    expected = "BRCA2:675|ZAR1L:345862" if clinvar_has_geneinfo else ""
    assert [(r["pos"], r["GENEINFO"]) for r in rows] == [("100", expected)]


@needs_bcftools
def test_command_line_thresholds_override_the_defaults(tmp_path):
    sample = _vcf(tmp_path / "s.vcf.gz", CALLS[:2])
    out = tmp_path / "out"
    _run(tmp_path, "vcf", "clinvar", "--vcf-input", sample, "--clinvar-file",
         _clinvar(tmp_path), "--outdir", str(out), "--confidence-depth", "5", "6",
         "--confidence-gq", "10", "20")  # fmt: skip
    assert _labels(out / "clinvar_pathogenic.vcf.gz") == ["100:high:.", "200:high:."]


def test_thresholds_must_not_cross():
    with pytest.raises(UsageError, match="LOW <= HIGH"):
        Thresholds(min_depth=20, high_depth=10, min_gq=20, high_gq=30)


@needs_bcftools
def test_rare_variant_output_is_labelled(tmp_path):
    sample = _vcf(tmp_path / "s.vcf.gz", CALLS[:3])
    gnomad = _vcf(
        tmp_path / "gnomad.vcf.gz",
        ["100 . C T . . AF=0.0001 GT 0/1", "300 . G A . . AF=0.3 GT 0/1"],
        header='##INFO=<ID=AF,Number=A,Type=Float,Description="AF">\n',
    )
    out = tmp_path / "out"
    document = _run(tmp_path, "vcf", "gnomad", "--vcf-input", sample, "--gnomad-file",
                    gnomad, "--max-af", "0.01", "--outdir", str(out))  # fmt: skip
    # 100 is rare in gnomAD and 200 absent from it; 300 is common.
    assert _labels(out / "gnomad_af_lt_0.01.vcf.gz") == [
        "100:high:.",
        "200:low:low_depth",
    ]
    rows = _table(out / "gnomad_af_lt_0.01.confidence.tsv")
    assert [(r["pos"], r["GNOMAD_AF"]) for r in rows] == [
        ("100", "0.0001"),
        ("200", ""),
    ]
    roles = {o["role"]: o for o in document["outputs"]}
    assert {"rare_variants", "confidence_table"} <= set(roles)
    assert roles["rare_variants"]["metrics"]["variants"] == 2
    assert document["metrics"]["filtered_variants"] == 2
    codes = {w["code"] for w in document["warnings"]}
    assert "low_confidence_findings" in codes
    assert "unconfirmed_clinical_findings" not in codes


@needs_bcftools
def test_discovery_report_labels_likely_pathogenic_findings(tmp_path):
    annotated = _vcf(
        tmp_path / "chain_annotated.vcf.gz",
        [
            "100 a C T 50 PASS CLNSIG=Likely_pathogenic GT:AD:DP:GQ 0/1:15,15:30:99",
            "200 b A G 50 PASS CLNSIG=Benign;GNOMAD_AF=0.4 GT:AD:DP:GQ 0/1:3,3:6:40",
        ],
    )
    out = tmp_path / "significant_snp-indel_s.vcf.gz"
    with results.collecting(results.CommandResult(command="analyze")):
        assert run_discovery_filter(annotated, "snp-indel", str(out)) == 1
        result = results.current_result()
    assert _labels(out) == ["100:high:."]
    (row,) = _table(tmp_path / "significant_snp-indel_s.confidence.tsv")
    assert (row["CLNSIG"], row["confidence"]) == ("Likely_pathogenic", "high")
    codes = [w.code for w in result.warnings]
    assert codes == ["unconfirmed_clinical_findings"]


@needs_bcftools
def test_split_multiallelic_calls_are_not_judged_by_site_level_evidence(tmp_path):
    # A 1/2 call: annotation splits it into one record per ALT, which trims PL
    # to REF/that ALT (dropping the called 1/2) and copies DP4, whose alt reads
    # belong to both ALTs. Neither may make a well-supported call look bad.
    plain = tmp_path / "s.vcf"
    plain.write_text(
        "##fileformat=VCFv4.2\n"
        f"##contig=<ID=chr1,length={HG38_CHR1}>\n"
        "##bcftools_callCommand=call -mv\n"
        '##INFO=<ID=DP,Number=1,Type=Integer,Description="DP">\n'
        '##INFO=<ID=MQ,Number=1,Type=Float,Description="Average mapping quality">\n'
        '##INFO=<ID=DP4,Number=4,Type=Integer,Description="DP4">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="GT">\n'
        '##FORMAT=<ID=PL,Number=G,Type=Integer,Description="PL">\n'
        '##FORMAT=<ID=AD,Number=R,Type=Integer,Description="AD">\n'
        '##FORMAT=<ID=DP,Number=1,Type=Integer,Description="DP">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        + "".join(
            "chr1\t" + "\t".join(row.split()) + "\n"
            for row in (
                "100 . A G,T 200 . DP=30;MQ=60;DP4=0,0,15,15 GT:PL 1/2:255,255,255,255,0,255",
                "200 . A G,T 200 . DP=30;MQ=60;DP4=0,0,15,15 GT:PL:AD:DP "
                "1/2:255,255,255,255,0,255:0,15,15:30",
                "300 . A G,T 200 . DP=30;MQ=60;DP4=0,0,28,2 GT:PL:AD:DP "
                "1/2:255,255,255,255,0,255:0,28,2:30",
            )
        )
    )
    run_command(["bgzip", "-f", str(plain)])
    sites = _vcf(
        tmp_path / "clinvar.vcf.gz",
        [f"{pos} . A {alt} . . CLNSIG=Pathogenic GT 0/1"
         for pos in (100, 200, 300) for alt in "GT"],
    )  # fmt: skip
    out = tmp_path / "out"
    _run(tmp_path, "vcf", "clinvar", "--vcf-input", f"{plain}.gz",
         "--clinvar-file", sites, "--outdir", str(out))  # fmt: skip

    assert _labels(out / "clinvar_pathogenic.vcf.gz") == [
        # No allele counts or genotype quality apply: unknown, not low.
        "100:moderate:gq_unknown|allele_balance_unknown",
        "100:moderate:gq_unknown|allele_balance_unknown",
        # AD and FORMAT/DP give each ALT's own read fraction.
        "200:moderate:gq_unknown",
        "200:moderate:gq_unknown",
        # ... so a lopsided 1/2 call is still caught.
        "300:low:allele_imbalance|gq_unknown",
        "300:low:allele_imbalance|gq_unknown",
    ]


def _label(
    rows: list[str], header: str = "", mq: str = "MQ", **thresholds: float
) -> list[str]:
    t = Thresholds(**{"min_depth": 10, "high_depth": 20, "min_gq": 20,
                      "high_gq": 30, **thresholds})  # fmt: skip
    lines = _text(rows, header=header, mq=mq).splitlines()
    with results.collecting(results.CommandResult(command="test")):
        out = [x for x in label_records(lines, t) if isinstance(x, str)]
    labelled = []
    for line in out:
        if line.startswith("#"):
            continue
        info = dict(i.partition("=")[::2] for i in line.split("\t")[7].split(";"))
        labelled.append(f"{info.get('WGSE_CONF')}:{info.get('WGSE_CONF_REASONS')}")
    return labelled


def test_bcftools_calls_are_judged_by_likelihoods_and_strand_counts():
    header = "##bcftools_callCommand=call -mv\n"
    assert _label(
        [
            # Genotype quality from PL; DP4 alt reads all on one strand.
            "100 . C T 50 . DP4=12,13,0,15;MQ=60 GT:PL:AD 0/1:200,0,200:25,15",
            "200 . C T 50 . DP4=12,13,7,8;MQ=60 GT:PL:AD 0/1:200,0,200:25,15",
            "300 . C T 50 . DP4=12,13,7,8;MQ=60 GT:PL:AD 0/1:200,0,15:25,15",
            "400 . C T 50 . DP4=12,13,7,8;MQ=15 GT:PL:AD 0/1:200,0,200:25,15",
        ],
        header,
        mq="Average mapping quality",
    ) == [
        "low:strand_bias",
        "high:.",
        "low:low_gq",
        "low:low_mapq",
    ]


def test_mapping_quality_floor_follows_how_the_caller_averages_it():
    # bcftools' MQ is a mean, far below the RMS of the same reads.
    rows = [
        "100 . C T 50 . MQ=30 GT:AD:DP:GQ 0/1:15,15:30:99",
        "200 . C T 50 . MQ=15 GT:AD:DP:GQ 0/1:15,15:30:99",
    ]
    for rms in ("RMS Mapping Quality", "Root mean square mapping quality"):
        assert _label(rows, mq=rms) == ["low:low_mapq", "low:low_mapq"]
    assert _label(rows, mq="Average mapping quality") == ["high:.", "low:low_mapq"]


def test_quality_by_depth_is_judged_only_on_gatk_quality_scores():
    # A clean 34x heterozygote; DRAGEN's recalibrated QUAL gives it QD 1.5.
    row = ["100 . C T 50 PASS QD=1.5 GT:AD:DP:GQ 0/1:15,19:34:49"]
    gatk = "##GATKCommandLine=<ID=HaplotypeCaller>\n"
    dragen = "##DRAGENCommandLine=<ID=dragen>\n"
    assert _label(row, gatk) == ["low:low_qd"]
    assert _label(row, dragen) == ["high:."]


def test_haploid_and_deepvariant_style_calls():
    header = "##DeepVariant_version=1.6.1\n"
    assert _label(
        [
            # A hemizygous call needs half the depth of a diploid one.
            "100 . C T 50 PASS . GT:DP:GQ:VAF 1:7:40:1.0",
            "200 . C T 50 PASS . GT:DP:GQ:VAF 0/1:7:40:0.5",
            # DeepVariant's VAF stands in for allele counts.
            "300 . C T 50 PASS . GT:DP:GQ:VAF 0/1:30:40:0.12",
            # No metrics at all: nothing vouches for the call.
            "400 . C T 50 PASS . GT 1/1",
        ],
        header,
    ) == [
        "moderate:moderate_depth",
        "low:low_depth",
        "low:allele_imbalance",
        "low:no_call_metrics|depth_unknown|gq_unknown|allele_balance_unknown",
    ]


def test_relabelling_replaces_earlier_labels():
    rows = ["100 . C T 50 PASS . GT:AD:DP:GQ 0/1:15,15:30:99"]
    first = _label(rows)
    lines = _text(rows).splitlines()
    t = Thresholds(min_depth=40, high_depth=50, min_gq=20, high_gq=30)
    with results.collecting(results.CommandResult(command="test")):
        once = [x for x in label_records(lines, t) if isinstance(x, str)]
        twice = [x for x in label_records(once, t) if isinstance(x, str)]
    assert first == ["high:."]
    assert once == twice
    assert sum("ID=WGSE_CONF," in line for line in twice) == 1


def test_multisample_vcfs_are_judged_by_their_first_sample():
    text = _text(["100 . C T 50 PASS . GT:AD:DP:GQ 0/1:15,15:30:99"])
    text = text.replace("\tS\n", "\tS\tT\n").replace(":99\n", ":99\t0/0:30,0:30:99\n")
    t = Thresholds(min_depth=10, high_depth=20, min_gq=20, high_gq=30)
    with results.collecting(results.CommandResult(command="test")):
        summary = list(label_records(text.splitlines(), t))[-1]
        warnings = [w.code for w in results.current_result().warnings]
    assert summary.counts["high"] == 1
    assert warnings == ["confidence_first_sample_only"]


def test_multisample_site_totals_are_not_one_samples_evidence():
    # INFO/DP and DP4 pool both samples: alone they can't vouch for the first.
    text = _text(["100 . C T 50 PASS DP=60;DP4=15,15,15,15 GT:GQ 0/1:99"])
    text = text.replace("\tS\n", "\tS\tT\n").replace(":99\n", ":99\t0/0:99\n")
    t = Thresholds(min_depth=10, high_depth=20, min_gq=20, high_gq=30)
    with results.collecting(results.CommandResult(command="test")):
        (finding,) = list(label_records(text.splitlines(), t))[-1].findings
    assert finding.confidence.label == "moderate"
    assert finding.confidence.reasons == ["depth_unknown", "allele_balance_unknown"]
