"""Round trip from professional pipelines (#354): nf-core/sarek's joint-called
multi-sample VCF and DeepVariant gVCF become microarray kits, and the results
reach MultiQC as custom content."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.htslib_helpers import bgzip_index, needs_htslib
from tests.smoke_utils import state_directory_setting
from tests.test_microarray_library import _library

pytestmark = needs_htslib

HEADER = (
    "##fileformat=VCFv4.2\n"
    "##contig=<ID={contig},length=5000>\n"
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
    '##FORMAT=<ID=GQ,Number=1,Type=Integer,Description="Genotype quality">\n'
    '##INFO=<ID=END,Number=1,Type=Integer,Description="End of the block">\n'
    "##source=nf-core/sarek\n"
)


def _vcf(
    path: Path, samples: list[str], records: list[str], contig: str = "chr1"
) -> str:
    columns = "\t".join(["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER"])
    path.write_text(
        HEADER.format(contig=contig)
        + f"{columns}\tINFO\tFORMAT\t"
        + "\t".join(samples)
        + "\n"
        + "".join(record + "\n" for record in records)
    )
    return str(bgzip_index(path))


def _microarray(config: Path, vcf: str, fasta: Path, outdir: Path, *extra: str):
    return subprocess.run(
        [sys.executable, "-m", "wgsextract_cli.main", "--config", str(config)]
        + ["microarray", "--formats", "23andme_v3", "--input", vcf]
        + ["--ref", str(fasta), "--outdir", str(outdir), *extra],
        capture_output=True,
        text=True,
    )


def _kit(path: Path) -> dict[str, str]:
    return {
        line.split("\t")[0]: line.split("\t")[3].strip()
        for line in path.read_text().splitlines()
        if not line.startswith("#")
    }


@pytest.fixture
def library(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "reference"
    fasta, seq = _library(root)
    config = tmp_path / "config.toml"
    config.write_text(
        f'reference_library = "{root.as_posix()}"\n' + state_directory_setting(tmp_path)
    )
    return config, fasta, seq


def test_joint_vcf_needs_a_sample_and_exports_only_that_sample(tmp_path, library):
    config, fasta, _seq = library
    # Markers rs101/rs102/rs103 sit at chr1:1000/2000/3000 on reference A.
    joint = _vcf(
        tmp_path / "joint_germline.vcf",
        ["P1_N", "P2_N"],
        [
            "chr1\t1000\t.\tA\tC\t50\tPASS\t.\tGT:GQ\t1/1:40\t0/1:40",
            "chr1\t2000\t.\tA\tC,G\t50\tPASS\t.\tGT:GQ\t0/2:40\t1/1:40",
            "chr1\t3000\t.\tA\tT\t50\tPASS\t.\tGT:GQ\t0/1:40\t0/0:40",
        ],
    )
    outdir = tmp_path / "out"

    refused = _microarray(config, joint, fasta, outdir)
    assert refused.returncode != 0
    assert "choose one with --sample (P1_N, P2_N)" in refused.stderr
    missing = _microarray(config, joint, fasta, outdir, "--sample", "P3_N")
    assert missing.returncode != 0 and "not in" in missing.stderr

    done = _microarray(config, joint, fasta, outdir, "--sample", "P2_N")
    assert done.returncode == 0, done.stderr
    assert _kit(outdir / "joint_germline_P2_N_23andMe_V3.txt") == {
        "rs101": "AC",
        "rs102": "CC",
        "rs103": "AA",
    }
    assert not list(outdir.glob("Sample_*"))
    manifest = json.loads(
        (outdir / "joint_germline_P2_N_microarray.manifest.json").read_text()
    )
    assert manifest["metrics"]["sample"] == "P2_N"

    # ... and the kit's summary reaches MultiQC under the sample's name.
    subprocess.run(
        [sys.executable, "-m", "wgsextract_cli.main", "--config", str(config)]
        + ["qc", "multiqc-metrics", "--input", str(outdir)],
        check=True,
    )
    rows = json.loads((outdir / "wgsextract_microarray_mqc.json").read_text())
    assert rows["data"]["P2_N"]["autosomal_called"] > 0


@pytest.mark.parametrize(
    ("contig", "block", "end"),
    [("chr1", "<*>", 1999), ("1", ".", 2000)],
    ids=["deepvariant", "dragen-no-chr"],
)
def test_gvcf_reference_blocks_are_calls_and_gaps_are_no_calls(
    tmp_path, library, contig, block, end
):
    config, fasta, seq = library
    # rs101 in a 0/0 block, rs102 a variant, rs103 in a ./. block (no
    # coverage). DRAGEN genome VCFs write blocks with ALT "." and, like
    # GRCh37 pipelines, may name contigs without "chr" unlike the FASTA. Its
    # blocks can also overlap the next record, which then outranks the block.
    gvcf = _vcf(
        tmp_path / "P1_N.deepvariant.g.vcf",
        ["P1_N"],
        [
            f"{contig}\t1\t.\t{seq[0]}\t{block}\t0\t.\tEND={end}\tGT:GQ\t0/0:50",
            f"{contig}\t2000\t.\tA\tC,<*>\t40\tPASS\t.\tGT:GQ\t1/1:40",
            f"{contig}\t2001\t.\t{seq[2000]}\t{block}\t0\t.\tEND=2500\tGT:GQ\t0/0:50",
            f"{contig}\t2501\t.\t{seq[2500]}\t{block}\t0\t.\tEND=3500\tGT:GQ\t./.:0",
        ],
        contig=contig,
    )
    outdir = tmp_path / "out"
    done = _microarray(config, gvcf, fasta, outdir)
    assert done.returncode == 0, done.stderr
    # Named like the input, so the gVCF's kit can't replace its VCF's.
    assert _kit(outdir / "P1_N.deepvariant.g_23andMe_V3.txt") == {
        "rs101": "AA",
        "rs102": "CC",
        "rs103": "--",
    }


@pytest.mark.parametrize(
    ("cli", "configured", "expected"),
    [
        (None, {}, False),
        (None, {"microarray_assume_hom_ref": True}, None),
        (True, {}, True),
    ],
    ids=["gvcf-default", "config-kept", "cli-kept"],
)
def test_gvcf_no_call_default_yields_to_cli_and_config(
    tmp_path, monkeypatch, cli, configured, expected
):
    import argparse

    from wgsextract_cli.commands import microarray
    from wgsextract_cli.core.config import settings

    gvcf = _vcf(
        tmp_path / "S.g.vcf",
        ["S"],
        ["chr1\t1\t.\tA\t<*>\t0\t.\tEND=10\tGT:GQ\t0/0:50"],
    )
    monkeypatch.setattr(microarray, "settings", {**settings, **configured})
    args = argparse.Namespace(input=gvcf, sample=None, assume_hom_ref=cli)
    assert microarray._vcf_input_layout(args) == (None, True)
    # None leaves quality_policy to read the configured value.
    assert args.assume_hom_ref is expected
