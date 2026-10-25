import gzip
import subprocess

from smoke_utils import run_cli

from tests.htslib_helpers import bgzip_index
from wgsextract_cli.core.pharmgkb import build_table, write_table

VARIANTS = (
    "Variant ID\tVariant Name\tGene IDs\tGene Symbols\tLocation\tSynonyms\n"
    "PA1\trs100\tPA9\tCYP2C19\tNC_000010.11:94761900\t"
    "12248560, NC_000010.10:g.96521657C>T, NC_000010.11:g.94761900C>T, "
    "NC_000010.11:g.94761900C>G, NG_008384.3:g.4195C>T\n"
    "PA2\trs200\tPA8\tVKORC1\tNC_000016.10:31096368\tNC_000016.10:g.31096368C>T\n"
    "PA3\trs300\tPA7\tOLD\tNC_000004.10:5000\tNC_000004.10:g.5000A>G\n"
    "PA4\trs400\t\t\t\t\n"
    "PA5\trs500\tPA6\tDEL\tNC_000001.11:97593254\t"
    "NC_000001.11:g.97593251_97593254=, NC_000001.11:g.97593254del\n"
)
CLINICAL = (
    "Clinical Annotation ID\tVariant/Haplotypes\tGene\tLevel of Evidence\t"
    "Phenotype Category\tDrug(s)\n"
    "1\trs200\tVKORC1\t1A\tDosage\twarfarin\n"
    "2\trs100\tCYP2C19\t1A\tMetabolism/PK\tclopidogrel;omeprazole\n"
    "3\trs100\tCYP2C19\t3\tToxicity\tsome drug, other=x\n"
    "4\tCYP2D6*4\tCYP2D6\t1A\tEfficacy\tcodeine\n"
    "5\trs300\tOLD\t3\tEfficacy\tx\n"
    "6\trs400\tNONE\t3\tEfficacy\ty\n"
    "7\trs500\tDEL\t3\tEfficacy\tz\n"
)
RS100 = (
    "rs100|CYP2C19|1A|Metabolism/PK|clopidogrel/omeprazole,"
    "rs100|CYP2C19|3|Toxicity|some_drug/_other:x"
)


def test_build_table_lists_each_hg38_snv_allele():
    assert build_table(CLINICAL, VARIANTS) == [
        ("chr10", 94761900, "C", "G", RS100),
        ("chr10", 94761900, "C", "T", RS100),
        ("chr16", 31096368, "C", "T", "rs200|VKORC1|1A|Dosage|warfarin"),
    ]


def test_vcf_pharmgkb_annotates_matching_alleles_only(tmp_path):
    library = tmp_path / "reflib"
    (library / "ref").mkdir(parents=True)
    # A library resolves annotation data only alongside a genome.
    (library / "genomes").mkdir()
    (library / "genomes" / "hg38.fa").write_text(">chr10\nACGT\n")
    subprocess.run(["bgzip", "-f", str(library / "genomes" / "hg38.fa")], check=True)
    write_table(
        build_table(CLINICAL, VARIANTS), str(library / "ref" / "pharmgkb_hg38.tsv.gz")
    )
    vcf = tmp_path / "sample.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n"
        '##INFO=<ID=END,Number=1,Type=Integer,Description="End">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "##contig=<ID=chr1,length=248956422>\n"
        "##contig=<ID=chr10,length=133797422>\n"
        "##contig=<ID=chr16,length=90338345>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        "chr10\t94761900\t.\tC\tT\t50\tPASS\t.\tGT\t1/1\n"
        # A reference block spanning rs200's site, and another allele at it.
        "chr16\t31096000\t.\tG\t.\t50\tPASS\tEND=31096367\tGT\t0/0\n"
        "chr16\t31096368\t.\tC\tA\t50\tPASS\t.\tGT\t0/1\n"
    )
    vcf_gz = bgzip_index(vcf)

    rc, _, stderr = run_cli(
        [
            "vcf",
            "pharmgkb",
            "--input",
            vcf_gz,
            "--ref",
            str(library),
            "--outdir",
            str(tmp_path / "out"),
        ]  # fmt: skip
    )

    assert rc == 0, stderr
    with gzip.open(tmp_path / "out" / "pharmgkb_annotated.vcf.gz", "rt") as out:
        info = [line.split("\t")[7] for line in out if not line.startswith("#")]
    assert info == [f"PHARMGKB={RS100}", "END=31096367", "."]


def test_vcf_pharmgkb_on_grch37_says_the_data_is_grch38(tmp_path):
    library = tmp_path / "reflib"
    (library / "ref").mkdir(parents=True)
    (library / "genomes").mkdir()
    write_table(
        build_table(CLINICAL, VARIANTS), str(library / "ref" / "pharmgkb_hg38.tsv.gz")
    )
    vcf = tmp_path / "sample.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "##contig=<ID=1,length=249250621>\n"
        "##contig=<ID=10,length=135534747>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        "10\t96521657\t.\tC\tT\t50\tPASS\t.\tGT\t1/1\n"
    )

    rc, _, stderr = run_cli(
        [
            "vcf",
            "pharmgkb",
            "--input",
            bgzip_index(vcf),
            "--ref",
            str(library),
            "--outdir",
            str(tmp_path / "out"),
        ]  # fmt: skip
    )

    assert rc != 0
    assert "placed on GRCh38" in stderr
    assert "run 'ref pharmgkb'" not in stderr
