import json
import logging
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from tests.htslib_helpers import bgzip_index, needs_htslib, run_wgsextract, write_fasta
from wgsextract_cli.commands.vcf import pharmcat
from wgsextract_cli.commands.vcf.pharmcat import (
    REFERENCE_FASTA,
    merge_outside_calls,
    pharmcat_directory,
    prepare_input,
    summarize,
)
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import WGSExtractError, run_command


def test_a_missing_pharmcat_install_says_where_to_get_it(tmp_path):
    lib = ReferenceLibrary(str(tmp_path))
    with pytest.raises(WGSExtractError) as error:
        pharmcat_directory(Namespace(pharmcat_dir=None), lib)
    message = str(error.value)
    assert str(tmp_path / "pharmcat") in message
    assert "github.com/PharmGKB/PharmCAT/releases" in message
    assert "pharmcat_directory" in message and "--pharmcat-dir" in message


def test_an_extracted_release_in_the_library_is_found(tmp_path):
    release = tmp_path / "pharmcat"
    release.mkdir()
    for name in ("pharmcat_pipeline", "pharmcat.jar", "pharmcat_regions.bed"):
        (release / name).write_text("x")
    lib = ReferenceLibrary(str(tmp_path))
    assert pharmcat_directory(Namespace(pharmcat_dir=None), lib) == str(release)


def test_summary_lists_called_genes_and_skips_unknown(tmp_path):
    phenotype = tmp_path / "s.phenotype.json"
    phenotype.write_text(
        json.dumps(
            {
                "geneReports": {
                    "CYP2C19": {
                        "recommendationDiplotypes": [
                            {
                                "allele1": {"name": "*17"},
                                "allele2": {"name": "*17"},
                                "phenotypes": ["Ultrarapid Metabolizer"],
                            }
                        ]
                    },
                    "CYP2D6": {
                        "recommendationDiplotypes": [
                            {
                                "allele1": {"name": "Unknown"},
                                "allele2": {"name": "Unknown"},
                            }
                        ]
                    },
                }
            }
        )
    )
    assert summarize(str(phenotype)) == [
        ("CYP2C19", "*17/*17", "Ultrarapid Metabolizer")
    ]


def test_outside_calls_merge_and_reject_a_gene_called_twice(tmp_path):
    cyp2d6 = tmp_path / "s.cyp2d6.pharmcat.tsv"
    cyp2d6.write_text("CYP2D6\t*1/*2≥3\n", encoding="utf-8")
    hla = tmp_path / "s.hla.pharmcat.tsv"
    hla.write_text("# T1K\nHLA-A\t*02:01/*24:02\nHLA-B\t*07:02/*57:01", "utf-8")
    merged = tmp_path / "merged.tsv"

    merge_outside_calls([str(cyp2d6), str(hla)], str(merged))
    assert merged.read_text(encoding="utf-8") == (
        "CYP2D6\t*1/*2≥3\nHLA-A\t*02:01/*24:02\nHLA-B\t*07:02/*57:01\n"
    )
    with pytest.raises(WGSExtractError, match="CYP2D6 is called in both"):
        merge_outside_calls([str(cyp2d6), str(cyp2d6)], str(merged))


def test_an_outside_call_file_that_does_not_exist_is_named(tmp_path):
    with pytest.raises(WGSExtractError, match="Outside-call file not found"):
        merge_outside_calls([str(tmp_path / "nope.tsv")], str(tmp_path / "m.tsv"))
    assert not (tmp_path / "m.tsv").exists()


# --- the command, with a stand-in PharmCAT release --------------------------------

# Records how it was run, the VCF it was given and the outside calls beside its
# output, then writes the outputs the real pipeline does (or exits with the
# code in an `exit` file beside it).
FAKE_PIPELINE = """\
import json, os, shutil, sys
from pathlib import Path

here = Path(__file__).parent
args = sys.argv[1:]
out, bf = Path(args[args.index("-o") + 1]), args[args.index("-bf") + 1]
outside = out / f"{bf}.outside.tsv"
shutil.copy(args[0], here / "seen.vcf.gz")
(here / "run.json").write_text(json.dumps({
    "argv": args, "cwd": os.getcwd(),
    "outside": outside.read_text() if outside.exists() else None,
}))
if (here / "exit").exists():
    sys.exit(int((here / "exit").read_text()))
(out / f"{bf}.report.html").write_text("<html></html>")
(out / f"{bf}.phenotype.json").write_text(json.dumps({"geneReports": {"CYP2C19": {
    "recommendationDiplotypes": [{"allele1": {"name": "*1"}, "allele2": {
        "name": "*17"}, "phenotypes": ["Rapid Metabolizer"]}]}}}))
"""

REF_SEQS = {"chr1": "ACGT" * 100, "chr2": "TTGCA" * 80}


def _release(directory: Path) -> Path:
    """A PharmCAT release whose pipeline is FAKE_PIPELINE, with gene regions
    chr1:101-200 and chr2:11-50 and its GRCh38 reference already fetched."""
    directory.mkdir(parents=True)
    (directory / "pharmcat_pipeline").write_text(FAKE_PIPELINE)
    (directory / "pharmcat.jar").write_text("")
    (directory / "pharmcat_regions.bed").write_text(
        "chr1\t100\t200\tPX=CYP2C19\nchr2\t10\t50\tPX=DPYD\n"
    )
    plain = write_fasta(directory / "reference.fna", REF_SEQS)
    with open(directory / REFERENCE_FASTA, "wb") as f:
        run_command(["bgzip", "-c", str(plain)], stdout=f)
    run_command(["samtools", "faidx", str(directory / REFERENCE_FASTA)])
    return directory


def _vcf(
    path: Path, body: str, build: str = "GRCh38", contig: str = "1", header: str = ""
) -> str:
    """A bgzipped, indexed single-sample VCF on ``contig`` (body uses {c})."""
    plain = path.with_suffix("")
    plain.write_text(
        "##fileformat=VCFv4.2\n"
        f"##reference={build}\n" + header + f"##contig=<ID={contig},length=400>\n"
        '##INFO=<ID=DP,Number=1,Type=Integer,Description="d">\n'
        '##INFO=<ID=END,Number=1,Type=Integer,Description="e">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="g">\n'
        '##FORMAT=<ID=AD,Number=R,Type=Integer,Description="a">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tHG002\n"
        + body.format(c=contig)
    )
    return bgzip_index(plain, path)


def _base(pos: int) -> str:
    return REF_SEQS["chr1"][pos - 1]


def _view(vcf: str | Path, *options: str) -> list[str]:
    out = run_command(["bcftools", "view", *options, str(vcf)], capture_output=True)
    return out.stdout.splitlines()


# A SNP inside chr1's gene region, and one outside every region.
SNPS = (
    f"{{c}}\t150\t.\t{_base(150)}\tT\t50\tPASS\tDP=20\tGT:AD\t0/1:10,10\n"
    f"{{c}}\t300\t.\t{_base(300)}\tT\t50\tPASS\tDP=20\tGT:AD\t1/1:0,20\n"
)


@pytest.fixture
def pharmcat_env(monkeypatch):
    """PharmCAT's Pixi environment, as the running Python."""
    monkeypatch.setattr(pharmcat, "pixi_env_tool", lambda _tool: sys.executable)


@needs_htslib
def test_pharmcat_runs_its_pipeline_on_the_gene_regions_in_its_names(
    tmp_path, pharmcat_env, caplog
):
    release = _release(tmp_path / "pharmcat")
    vcf = _vcf(tmp_path / "HG002.hard-filtered.vcf.gz", SNPS)
    out = tmp_path / "out"
    caplog.set_level(logging.INFO)
    run_wgsextract("vcf", "pharmcat", "--vcf-input", vcf, "--pharmcat-dir",
                   str(release), "--outdir", str(out), "--absent-to-ref")  # fmt: skip

    run = json.loads((release / "run.json").read_text())
    prepared = Path(run["argv"][0])
    assert prepared.name == "prepared.vcf.gz"
    assert prepared.parent.parent == out
    assert run["argv"][1:] == ["-o", str(out), "-bf", "HG002.hard-filtered", "-reporterHtml",
                               "-reporterJson", "--absent-to-ref"]  # fmt: skip
    assert run["cwd"] == str(out)
    assert run["outside"] is None
    # Only the region's SNP, renamed to chr1, without INFO and with only GT.
    assert _view(release / "seen.vcf.gz", "-H") == [
        f"chr1\t150\t.\t{_base(150)}\tT\t50\tPASS\t.\tGT\t0/1"
    ]
    assert sorted(p.name for p in out.iterdir()) == [
        "HG002.hard-filtered.phenotype.json", "HG002.hard-filtered.report.html"
    ]  # fmt: skip
    assert "CYP2C19   *1/*17" in caplog.text and "Rapid Metabolizer" in caplog.text
    assert f"PharmCAT report: {out / 'HG002.hard-filtered.report.html'}" in caplog.text
    assert "pass them with --outside-call" in caplog.text


@needs_htslib
def test_pharmcat_takes_an_uncompressed_vcf(tmp_path, pharmcat_env):
    release = _release(tmp_path / "pharmcat")
    plain = tmp_path / "s.vcf"
    plain.write_text("\n".join(_view(_vcf(tmp_path / "gz.vcf.gz", SNPS))) + "\n")
    run_wgsextract("vcf", "pharmcat", "--vcf-input", str(plain), "--pharmcat-dir",
                   str(release))  # fmt: skip
    assert len(_view(release / "seen.vcf.gz", "-H")) == 1


@needs_htslib
def test_pharmcat_input_expands_gvcf_blocks_to_reference_positions(tmp_path):
    release = _release(tmp_path / "pharmcat")
    work = tmp_path / "work"
    work.mkdir()
    vcf = _vcf(
        tmp_path / "s.g.vcf.gz",
        f"chr1\t95\t.\t{_base(95)}\t<*>\t.\t.\tEND=103\tGT\t0/0\n"
        f"chr1\t104\t.\t{_base(104)}\tT,<*>\t50\t.\tDP=9\tGT:AD\t0/1:5,4,0\n",
        contig="chr1",
    )
    prepared = prepare_input(vcf, str(release), str(release / REFERENCE_FASTA),
                             str(work))  # fmt: skip
    rows = [line.split("\t") for line in _view(prepared, "-H")]
    assert [(r[1], r[3], r[4], r[9]) for r in rows] == [
        *((str(p), _base(p), "<*>", "0/0") for p in range(95, 104)),
        ("104", _base(104), "T,<*>", "0/1"),
    ]


@needs_htslib
def test_pharmcat_input_from_a_gatk_gvcf_is_no_longer_a_gvcf(tmp_path):
    release = _release(tmp_path / "pharmcat")
    work = tmp_path / "work"
    work.mkdir()
    vcf = _vcf(
        tmp_path / "s.g.vcf.gz",
        f"chr1\t95\t.\t{_base(95)}\t<NON_REF>\t.\t.\tEND=120\tGT\t0/0\n",
        contig="chr1",
        header="##GVCFBlock0-20=minGQ=0(inclusive),maxGQ=20(exclusive)\n",
    )
    prepared = prepare_input(vcf, str(release), str(release / REFERENCE_FASTA),
                             str(work))  # fmt: skip
    assert not [line for line in _view(prepared, "-h") if "GVCFBlock" in line]


@needs_htslib
def test_pharmcat_merges_outside_calls_where_its_pipeline_looks(
    tmp_path, pharmcat_env, caplog
):
    release = _release(tmp_path / "pharmcat")
    vcf = _vcf(tmp_path / "s.vcf.gz", SNPS)
    cyp2d6 = tmp_path / "s.cyp2d6.pharmcat.tsv"
    cyp2d6.write_text("CYP2D6\t*1/*4\n")
    hla = tmp_path / "s.hla.pharmcat.tsv"
    hla.write_text("HLA-B\t*57:01/*07:02\n")
    caplog.set_level(logging.INFO)
    run_wgsextract("vcf", "pharmcat", "--vcf-input", vcf, "--pharmcat-dir",
                   str(release), "--outside-call", str(cyp2d6), "--outside-call",
                   str(hla))  # fmt: skip

    run = json.loads((release / "run.json").read_text())
    assert run["outside"] == "CYP2D6\t*1/*4\nHLA-B\t*57:01/*07:02\n"
    assert "--absent-to-ref" not in run["argv"]
    assert "pass them with --outside-call" not in caplog.text


@needs_htslib
def test_pharmcat_warns_about_outside_calls_left_from_a_previous_run(
    tmp_path, pharmcat_env, caplog
):
    release = _release(tmp_path / "pharmcat")
    vcf = _vcf(tmp_path / "s.vcf.gz", SNPS)
    (tmp_path / "s.outside.tsv").write_text("CYP2D6\t*1/*1\n")
    run_wgsextract("vcf", "pharmcat", "--vcf-input", vcf, "--pharmcat-dir",
                   str(release))  # fmt: skip

    run = json.loads((release / "run.json").read_text())
    assert run["outside"] == "CYP2D6\t*1/*1\n"
    assert "will also use the outside calls" in caplog.text
    assert "pass them with --outside-call" not in caplog.text


@needs_htslib
def test_a_failed_pharmcat_run_is_an_error_and_leaves_no_work_files(
    tmp_path, pharmcat_env
):
    release = _release(tmp_path / "pharmcat")
    (release / "exit").write_text("3")
    vcf = _vcf(tmp_path / "s.vcf.gz", SNPS)
    out = tmp_path / "out"
    with pytest.raises(WGSExtractError, match=r"PharmCAT failed \(exit 3\)"):
        run_wgsextract("vcf", "pharmcat", "--vcf-input", vcf, "--pharmcat-dir",
                       str(release), "--outdir", str(out))  # fmt: skip
    assert list(out.iterdir()) == []


@needs_htslib
def test_pharmcat_refuses_a_grch37_vcf(tmp_path, pharmcat_env):
    _release(tmp_path / "lib" / "pharmcat")
    vcf = _vcf(tmp_path / "s.vcf.gz", SNPS, build="GRCh37")
    with pytest.raises(WGSExtractError, match="needs a GRCh38 VCF; .* is hg19"):
        run_wgsextract("vcf", "pharmcat", "--vcf-input", vcf, "--ref",
                       str(tmp_path / "lib"))  # fmt: skip
    assert not (tmp_path / "lib" / "pharmcat" / "run.json").exists()


@needs_htslib
def test_pharmcat_refuses_a_grch37_vcf_without_a_reference_library(
    tmp_path, pharmcat_env
):
    release = _release(tmp_path / "pharmcat")
    vcf = _vcf(tmp_path / "s.vcf.gz", SNPS, build="GRCh37")
    with pytest.raises(WGSExtractError, match="needs a GRCh38 VCF"):
        run_wgsextract("vcf", "pharmcat", "--vcf-input", vcf, "--pharmcat-dir",
                       str(release))  # fmt: skip


@needs_htslib
def test_pharmcat_without_its_environment_says_how_to_install_it(tmp_path, monkeypatch):
    monkeypatch.setattr(pharmcat, "pixi_env_tool", lambda _tool: None)
    release = _release(tmp_path / "pharmcat")
    vcf = _vcf(tmp_path / "s.vcf.gz", SNPS)
    with pytest.raises(WGSExtractError, match="pixi install -e pharmcat"):
        run_wgsextract("vcf", "pharmcat", "--vcf-input", vcf, "--pharmcat-dir",
                       str(release))  # fmt: skip


def test_pharmcats_reference_is_fetched_once_with_its_own_downloader(tmp_path):
    (tmp_path / "pcat").mkdir()
    (tmp_path / "pcat" / "__init__.py").write_text(
        "from pathlib import Path\n"
        "def download_reference_fasta_and_index(d):\n"
        f"    Path(d, {REFERENCE_FASTA!r}).write_text('fetched ' + d)\n"
    )
    reference = pharmcat._ensure_reference([sys.executable], str(tmp_path))
    assert reference == str(tmp_path / REFERENCE_FASTA)
    assert Path(reference).read_text() == f"fetched {tmp_path}"
    (tmp_path / "pcat" / "__init__.py").write_text("raise SystemExit(1)\n")
    assert pharmcat._ensure_reference([sys.executable], str(tmp_path)) == reference
