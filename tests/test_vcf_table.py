import csv
import json
import sqlite3
from pathlib import Path

import pytest

from tests.htslib_helpers import bgzip_index, needs_htslib, run_wgsextract
from wgsextract_cli.core.results import CommandResult, collecting
from wgsextract_cli.core.utils import run_command
from wgsextract_cli.core.variant_table import (
    COLUMN_NAMES,
    parse_header,
    variant_rows,
    vep_allele,
)

CSQ = (
    "Allele|Consequence|IMPACT|SYMBOL|Gene|Feature_type|Feature|BIOTYPE|HGVSc|"
    "HGVSp|Existing_variation|CANONICAL|MANE_SELECT|gnomADg_AF|CLIN_SIG"
)
ANN = (
    "Allele | Annotation | Annotation_Impact | Gene_Name | Gene_ID | Feature_Type "
    "| Feature_ID | Transcript_BioType | Rank | HGVS.c | HGVS.p"
)
HEADER = f"""##fileformat=VCFv4.2
##contig=<ID=chr1,length=248956422>
##FILTER=<ID=LowQual,Description="Low quality">
##INFO=<ID=DP,Number=1,Type=Integer,Description="Depth">
##INFO=<ID=END,Number=1,Type=Integer,Description="End">
##INFO=<ID=CLNSIG,Number=.,Type=String,Description="ClinVar significance">
##INFO=<ID=CLNDN,Number=.,Type=String,Description="ClinVar disease name">
##INFO=<ID=GNOMAD_AF,Number=A,Type=Float,Description="gnomAD Allele Frequency">
##INFO=<ID=PHARMGKB,Number=.,Type=String,Description="ClinPGx (PharmGKB) clinical annotations: rsID|gene|level of evidence|phenotype category|drugs">
##INFO=<ID=WGSE_CONF,Number=A,Type=String,Description="Confidence">
##INFO=<ID=WGSE_CONF_REASONS,Number=A,Type=String,Description="Reasons">
##INFO=<ID=CSQ,Number=.,Type=String,Description="Consequence annotations from Ensembl VEP. Format: {CSQ}">
##INFO=<ID=ANN,Number=.,Type=String,Description="Functional annotations: '{ANN}' ">
##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">
##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Depth">
##FORMAT=<ID=AD,Number=R,Type=Integer,Description="Allelic depths">
##FORMAT=<ID=GQ,Number=1,Type=Integer,Description="Genotype quality">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1
"""


def _record(*fields: str) -> str:
    return "\t".join(fields) + "\n"


RECORDS = [
    # Two transcripts: the MANE Select one is chosen over the canonical one.
    _record(
        "chr1",
        "100",
        "rs1",
        "A",
        "G",
        "50",
        "PASS",
        "DP=30;CLNSIG=Pathogenic;CLNDN=Some_disease;GNOMAD_AF=0.001;"
        "WGSE_CONF=high;WGSE_CONF_REASONS=moderate_depth|moderate_gq;CSQ="
        "G|intron_variant|MODIFIER|GENEA|ENSG1|Transcript|ENST1|protein_coding|"
        "ENST1.1:c.10+5A>G||rs1|YES|||,"
        "G|missense_variant|MODERATE|GENEA|ENSG1|Transcript|ENST2|protein_coding|"
        "ENST2.1:c.20A>G|ENSP2.1:p.Lys7%3D|rs1||NM_2.1||",
        "GT:DP:AD:GQ",
        "0/1:28:14,14:99",
    ),
    # A multi-allelic indel: VEP drops the shared first base, naming the
    # deletion "-" and the insertion "TT".
    _record(
        "chr1",
        "200",
        ".",
        "AT",
        "A,ATT",
        "40",
        "PASS",
        "GNOMAD_AF=0.1,0.2;CSQ="
        "-|frameshift_variant|HIGH|GENEB|ENSG2|Transcript|ENST3|protein_coding|"
        "ENST3.1:c.5del||rs2&COSV1|YES|||,"
        "TT|inframe_insertion|MODERATE|GENEB|ENSG2|Transcript|ENST3|protein_coding|"
        "ENST3.1:c.5dup||rs3|YES|||",
        "GT:AD:GQ",
        "1/2:0,9,8:40",
    ),
    # The sample carries only the first ALT.
    _record(
        "chr1",
        "300",
        ".",
        "C",
        "T,G",
        "60",
        "PASS",
        ".",
        "GT:DP:GQ",
        "0/1:20:50",
    ),
    # A gVCF reference block: no variant.
    _record(
        "chr1",
        "400",
        ".",
        "G",
        "<NON_REF>",
        ".",
        ".",
        "END=500",
        "GT:DP:GQ",
        "0/0:25:60",
    ),
    # SnpEff only; depth from AD; no QUAL.
    _record(
        "chr1",
        "500",
        "rs5;rs6",
        "C",
        "T",
        ".",
        "LowQual",
        "ANN=T|stop_gained|HIGH|GENEC|GENEC|transcript|NM_5|protein_coding|"
        "2/5|c.100C>T|p.Gln34*",
        "GT:AD",
        "1/1:0,12",
    ),
    _record(
        "chr1",
        "600",
        ".",
        "C",
        "T",
        "30",
        "PASS",
        "DP=22;PHARMGKB=rs7|CYP2C19|3|Toxicity|warfarin/aspirin,"
        "rs7|CYP2C19|1A|Metabolism/PK|clopidogrel",
        "GT",
        "0/1",
    ),
    _record("chr1", "700", ".", "G", "A", "30", "PASS", ".", "GT", "1"),
    # The spanning-deletion placeholder '*' gets no row.
    _record("chr1", "800", ".", "A", "*,T", "30", "PASS", ".", "GT", "1/2"),
]


@pytest.fixture
def annotated_vcf(tmp_path: Path) -> str:
    plain = tmp_path / "sample.vcf"
    plain.write_text(HEADER + "".join(RECORDS))
    return bgzip_index(plain)


def _tsv(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        assert tuple(reader.fieldnames or ()) == COLUMN_NAMES
        return {(row["pos"], row["alt"]): row for row in reader}


@needs_htslib
def test_vcf_table_flattens_annotations_one_row_per_carried_allele(
    annotated_vcf, tmp_path
):
    phenotype = tmp_path / "sample.phenotype.json"
    phenotype.write_text(
        json.dumps(
            {
                "geneReports": {
                    "CYP2C19": {
                        "recommendationDiplotypes": [
                            {
                                "allele1": {"name": "*1"},
                                "allele2": {"name": "*2"},
                                "phenotypes": ["Intermediate Metabolizer"],
                            }
                        ]
                    }
                }
            }
        )
    )
    out = tmp_path / "out"
    result = CommandResult("vcf table")
    with collecting(result):
        run_wgsextract(
            "vcf", "table", "--vcf-input", annotated_vcf, "--outdir", str(out),
            "--pharmcat-json", str(phenotype),
        )  # fmt: skip

    rows = _tsv(out / "sample_variants.tsv")
    assert sorted(rows) == [
        ("100", "G"), ("200", "A"), ("200", "ATT"), ("300", "T"),
        ("500", "T"), ("600", "T"), ("700", "A"), ("800", "T"),
    ]  # fmt: skip

    mane = rows["100", "G"]
    assert (mane["rsid"], mane["gene"], mane["consequence"], mane["impact"]) == (
        "rs1", "GENEA", "missense_variant", "MODERATE",
    )  # fmt: skip
    assert (mane["hgvs_c"], mane["hgvs_p"]) == ("ENST2.1:c.20A>G", "ENSP2.1:p.Lys7=")
    assert (mane["clinvar_significance"], mane["clinvar_condition"]) == (
        "Pathogenic", "Some_disease",
    )  # fmt: skip
    assert (mane["gnomad_af"], mane["confidence"]) == ("0.001", "high")
    assert mane["confidence_reasons"] == "moderate_depth|moderate_gq"
    assert (mane["genotype"], mane["zygosity"], mane["depth"], mane["gq"]) == (
        "0/1", "het", "28", "99",
    )  # fmt: skip
    assert (mane["qual"], mane["filter"]) == ("50.0", "PASS")

    deletion, insertion = rows["200", "A"], rows["200", "ATT"]
    assert (deletion["consequence"], deletion["gnomad_af"], deletion["rsid"]) == (
        "frameshift_variant", "0.1", "rs2",
    )  # fmt: skip
    assert (insertion["consequence"], insertion["gnomad_af"]) == (
        "inframe_insertion", "0.2",
    )  # fmt: skip
    assert deletion["zygosity"] == insertion["zygosity"] == "het"
    assert deletion["depth"] == "17"  # the sum of AD

    snpeff = rows["500", "T"]
    assert (snpeff["rsid"], snpeff["gene"], snpeff["consequence"]) == (
        "rs5;rs6", "GENEC", "stop_gained",
    )  # fmt: skip
    assert (snpeff["hgvs_c"], snpeff["hgvs_p"], snpeff["qual"]) == (
        "c.100C>T", "p.Gln34*", "",
    )  # fmt: skip
    assert (snpeff["zygosity"], snpeff["depth"], snpeff["filter"]) == (
        "hom_alt", "12", "LowQual",
    )  # fmt: skip

    pgx = rows["600", "T"]
    assert (pgx["rsid"], pgx["gene"], pgx["pharmgkb_level"], pgx["pharmgkb_drugs"]) == (
        "rs7", "CYP2C19", "1A", "aspirin; clopidogrel; warfarin",
    )  # fmt: skip
    assert (pgx["pharmcat_diplotype"], pgx["pharmcat_phenotype"]) == (
        "*1/*2", "Intermediate Metabolizer",
    )  # fmt: skip
    assert pgx["depth"] == "22"  # INFO/DP when the sample has none
    assert rows["700", "A"]["zygosity"] == "hemizygous"

    db = sqlite3.connect(out / "sample_variants.sqlite")
    assert db.execute("SELECT count(*) FROM variants").fetchone() == (8,)
    assert db.execute(
        "SELECT typeof(pos), typeof(gnomad_af), typeof(depth), typeof(qual) "
        "FROM variants WHERE pos = 100"
    ).fetchone() == ("integer", "real", "integer", "real")
    assert db.execute(
        "SELECT count(*) FROM variants WHERE gene = 'GENEB' AND gnomad_af < 0.15"
    ).fetchone() == (1,)
    indexes = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {"variants_gene", "variants_rsid", "variants_position"} <= indexes
    plan = " ".join(
        str(row)
        for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM variants WHERE rsid = 'rs7'"
        )
    )
    assert "variants_rsid" in plan
    metadata = dict(db.execute("SELECT key, value FROM metadata"))
    assert metadata["sample"] == "S1"
    assert metadata["reference_build"] == "hg38"
    assert metadata["rows"] == "8"
    assert len(metadata["source_sha256"]) == 64
    assert metadata["annotations"] == "vep,snpeff,clinvar,gnomad,pharmgkb,confidence"
    described = [row[0] for row in db.execute("SELECT name FROM columns")]
    assert tuple(described) == COLUMN_NAMES
    db.close()

    roles = {Path(o.path).name: o.role for o in result.outputs}
    assert roles == {
        "sample_variants.tsv": "variant_table",
        "sample_variants.sqlite": "variant_database",
    }
    assert result.metrics["rows"] == 8


@needs_htslib
def test_vcf_table_all_alleles_keeps_alleles_the_sample_lacks(annotated_vcf, tmp_path):
    run_wgsextract(
        "vcf", "table", "--vcf-input", annotated_vcf, "--outdir", str(tmp_path),
        "--all-alleles", "--no-sqlite", "--region", "chr1:300-400",
    )  # fmt: skip
    rows = _tsv(tmp_path / "sample_variants.tsv")
    assert {key: row["zygosity"] for key, row in rows.items()} == {
        ("300", "T"): "het",
        ("300", "G"): "not_carried",
    }
    assert not (tmp_path / "sample_variants.sqlite").exists()


def _csq(allele: str, hgvsc: str, allele_num: str = "") -> str:
    return (
        f"{allele}|inframe_insertion|MODERATE|GENEB|ENSG2|Transcript|ENST3|"
        f"protein_coding|{hgvsc}||||||{allele_num}"
    )


def _rows(records: list[str], csq: str = CSQ, **kwargs) -> dict:
    header = parse_header(
        HEADER.replace(f"Format: {CSQ}", f"Format: {csq}").splitlines()
    )
    rows = (
        dict(zip(COLUMN_NAMES, row, strict=True))
        for row in variant_rows(records, header, **kwargs)
    )
    return {(str(row["pos"]), row["alt"]): row for row in rows}


def test_split_records_take_only_their_own_alleles_csq():
    # `vcf clinvar` splits A>AAA,AA into two records that both keep every
    # allele's CSQ entries, with the original ALLELE_NUM.
    entries = ",".join([_csq("AA", "c.5_6dup", "1"), _csq("A", "c.6dup", "2")])
    records = [
        _record("chr1", "200", ".", "A", alt, "40", "PASS", f"CSQ={entries}",
                "GT", "0/1")
        for alt in ("AAA", "AA")
    ]  # fmt: skip
    rows = _rows(records, CSQ + "|ALLELE_NUM")
    assert rows[("200", "AAA")]["hgvs_c"] == "c.5_6dup"
    assert rows[("200", "AA")]["hgvs_c"] == "c.6dup"


def test_split_record_never_takes_another_alleles_only_entries():
    # A>C,G split by `vcf clinvar`; VEP had annotated only C, so the G record
    # must not inherit C's gene just because every entry names one allele.
    entries = _csq("C", "c.1A>C")
    records = [
        _record("chr1", "300", ".", "A", alt, "40", "PASS",
                f"CSQ={entries};OLD_MULTIALLELIC=chr1|300|A|C,G|{i}", "GT", "0/1")
        for i, alt in enumerate(("C", "G"), start=1)
    ]  # fmt: skip
    rows = _rows(records)
    assert rows[("300", "C")]["gene"] == "GENEB"
    assert rows[("300", "G")]["gene"] is None
    assert rows[("300", "G")]["hgvs_c"] is None


def test_split_record_uses_its_original_allele_num():
    # The G record is the first (only) ALT of its split record but ALT 2 of
    # the original, which VEP's ALLELE_NUM counts.
    entries = ",".join([_csq("C", "c.1A>C", "1"), _csq("G", "c.1A>G", "2")])
    records = [
        _record("chr1", "400", ".", "A", alt, "40", "PASS",
                f"CSQ={entries};OLD_MULTIALLELIC=chr1|400|A|C,G|{i}", "GT", "0/1")
        for i, alt in enumerate(("C", "G"), start=1)
    ]  # fmt: skip
    rows = _rows(records, CSQ + "|ALLELE_NUM")
    assert rows[("400", "C")]["hgvs_c"] == "c.1A>C"
    assert rows[("400", "G")]["hgvs_c"] == "c.1A>G"


@pytest.mark.parametrize(
    ("ref", "alts", "csq_allele"),
    [
        # VEP leaves spanning deletions out of the shared-first-base test.
        ("GT", "G,*", "-"),
        # <*> is an ordinary allele to VEP, not a structural variant.
        ("GT", "G,<*>", "-"),
        ("A", "AT,<*>", "T"),
    ],
)
def test_star_alleles_dont_stop_vep_trimming(ref, alts, csq_allele):
    record = _record("chr1", "500", ".", ref, alts, "40", "PASS",
                     f"CSQ={_csq(csq_allele, 'c.9del')}", "GT", "1/2")  # fmt: skip
    rows = _rows([record])
    assert list(rows) == [("500", alts.split(",")[0])]
    assert rows[("500", alts.split(",")[0])]["hgvs_c"] == "c.9del"


def test_vep_allele_follows_vep():
    assert vep_allele("GT", ["G", "*"], "*") == "*"
    assert vep_allele("A", ["C", "<*>"], "C") == "C"
    assert vep_allele("GT", ["*"], "*") == "*"
    assert vep_allele("A", ["<DEL>", "AT"], "AT") == "AT"


def test_vep_spelling_wins_over_raw_alt_without_allele_num():
    # VEP names A>AAA,AA "AA" and "A": the raw spelling of the second ALT
    # ("AA") must not also pick up the first ALT's entry.
    entries = ",".join([_csq("AA", "c.5_6dup"), _csq("A", "c.6dup")])
    record = _record("chr1", "200", ".", "A", "AAA,AA", "40", "PASS",
                     f"CSQ={entries}", "GT", "1/2")  # fmt: skip
    rows = _rows([record])
    assert rows[("200", "AAA")]["hgvs_c"] == "c.5_6dup"
    assert rows[("200", "AA")]["hgvs_c"] == "c.6dup"


@pytest.mark.parametrize("fmt, sample", [("GT", "."), ("DP", "12")])
def test_missing_genotype_is_a_no_call(fmt, sample):
    record = _record("chr1", "700", ".", "G", "A", "30", "PASS", ".", fmt, sample)
    assert _rows([record]) == {}
    row = _rows([record], carried_only=False)[("700", "A")]
    assert row["zygosity"] == "no_call"
    sites_only = _rows([record], sample_index=None)[("700", "A")]
    assert sites_only["zygosity"] is None


@needs_htslib
def test_vcf_table_region_reads_a_bcf_in_place(annotated_vcf, tmp_path):
    bcf = tmp_path / "sample.bcf"
    run_command(["bcftools", "view", "-Ob", "-o", str(bcf), annotated_vcf])
    run_wgsextract(
        "vcf", "table", "--vcf-input", str(bcf), "--outdir", str(tmp_path / "o"),
        "--no-sqlite", "--region", "chr1:300-400",
    )  # fmt: skip
    rows = _tsv(tmp_path / "o" / "sample_variants.tsv")
    assert list(rows) == [("300", "T")]
    assert (tmp_path / "sample.bcf.csi").exists()
    assert not (tmp_path / "sample.bcf.gz").exists()
