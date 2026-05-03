import os
import subprocess

import pytest

from tests.smoke_utils import run_cli, verify_vcf


@pytest.fixture(scope="module")
def fake_data_dir(tmp_path_factory):
    """Fixture to generate fake genomics data once for the module."""
    tmp_dir = tmp_path_factory.mktemp("systematic_data")
    outdir = str(tmp_dir)

    # Force a specific reference path to ensure it's created in our tmp dir
    # and NOT resolved from a local reference library if one exists.
    ref_path = os.path.join(outdir, "systematic_fake_ref.fa")

    # Generate fake data: BAM, CRAM, VCF, and FASTQ
    # Use hg38 build, low coverage for speed
    rc, stdout, stderr = run_cli(
        [
            "qc",
            "fake-data",
            "--outdir",
            outdir,
            "--ref",
            ref_path,
            "--build",
            "hg38",
            "--coverage",
            "0.5",
            "--type",
            "all",
            "--seed",
            "123",
        ]
    )
    assert rc == 0, f"Fake data generation failed. STDOUT: {stdout}\nSTDERR: {stderr}"
    assert os.path.exists(ref_path), f"Fake reference was not created at {ref_path}"

    # Generate a dummy SNP tab for microarray testing
    # Format: CHROM\tPOS\tID
    snp_tab = os.path.join(outdir, "dummy_snps.tab")
    with open(snp_tab, "w") as f:
        f.write("#CHROM\tPOS\tID\tREF\tALT\n")
        f.write("chr1\t1000\trs123\tA\tG\n")
        f.write("chrM\t100\trs456\tC\tT\n")
        f.write("chrY\t500\trs789\tG\tA\n")

    # BGZIP and Index the SNP tab
    subprocess.run(["bgzip", snp_tab], check=True)
    subprocess.run(["tabix", "-p", "vcf", snp_tab + ".gz"], check=True)

    return outdir


def test_info_detailed(fake_data_dir):
    """Test 'info --detailed' on generated BAM."""
    bam_path = os.path.join(fake_data_dir, "fake.bam")
    rc, stdout, stderr = run_cli(["info", "--detailed", "--input", bam_path])
    assert rc == 0, f"STDOUT: {stdout}\nSTDERR: {stderr}"
    assert "Reference Genome" in stdout
    # Heuristic check for BAM info
    assert "BAM" in stdout or "BAM" in stderr
    # Should detect hg38 (from the MD5 sig or chromosome names)
    assert "hg38" in stdout.lower() or "grch38" in stdout.lower()


def test_bam_identify(fake_data_dir):
    """Test 'bam identify' on generated BAM and CRAM."""
    bam_path = os.path.join(fake_data_dir, "fake.bam")
    rc, stdout, stderr = run_cli(["bam", "identify", "--input", bam_path])
    assert rc == 0, f"STDOUT: {stdout}\nSTDERR: {stderr}"
    assert "MD5 Signature" in stdout or "MD5 Signature" in stderr

    cram_path = os.path.join(fake_data_dir, "fake.cram")
    # Identify might need --ref for CRAM to get header if not in cache,
    # but fake-data puts it in RG or CO which is usually visible.
    rc, stdout, stderr = run_cli(["bam", "identify", "--input", cram_path])
    assert rc == 0, f"STDOUT: {stdout}\nSTDERR: {stderr}"
    assert "MD5 Signature" in stdout or "MD5 Signature" in stderr


def test_extract_mito_vcf(fake_data_dir):
    """Test 'extract mito-vcf' on generated BAM."""
    bam_path = os.path.join(fake_data_dir, "fake.bam")
    ref_path = os.path.join(fake_data_dir, "systematic_fake_ref.fa")

    rc, stdout, stderr = run_cli(
        [
            "extract",
            "mito-vcf",
            "--input",
            bam_path,
            "--ref",
            ref_path,
            "--outdir",
            fake_data_dir,
        ]
    )
    assert rc == 0, f"STDOUT: {stdout}\nSTDERR: {stderr}"

    found_vcf = False
    for f in os.listdir(fake_data_dir):
        # Result filename usually contains MT or chrM and .vcf.gz
        if ("MT" in f or "chrM" in f) and f.endswith(".vcf.gz") and "temp" not in f:
            vcf_file = os.path.join(fake_data_dir, f)
            assert verify_vcf(vcf_file, allow_empty=True)
            found_vcf = True
            break
    assert found_vcf, f"Mito VCF not found. Files: {os.listdir(fake_data_dir)}"


def test_extract_ydna_vcf(fake_data_dir):
    """Test 'extract ydna-vcf' on generated BAM."""
    bam_path = os.path.join(fake_data_dir, "fake.bam")
    ref_path = os.path.join(fake_data_dir, "systematic_fake_ref.fa")

    rc, stdout, stderr = run_cli(
        [
            "extract",
            "ydna-vcf",
            "--input",
            bam_path,
            "--ref",
            ref_path,
            "--outdir",
            fake_data_dir,
        ]
    )
    assert rc == 0, f"STDOUT: {stdout}\nSTDERR: {stderr}"
    found_vcf = False
    for f in os.listdir(fake_data_dir):
        if ("_Y" in f or "chrY" in f) and f.endswith(".vcf.gz") and "temp" not in f:
            vcf_file = os.path.join(fake_data_dir, f)
            assert verify_vcf(vcf_file, allow_empty=True)
            found_vcf = True
            break
    assert found_vcf, f"Y-DNA VCF not found. Files: {os.listdir(fake_data_dir)}"


def test_vcf_snp_indel(fake_data_dir):
    """Test 'vcf snp' and 'vcf indel' on generated BAM."""
    bam_path = os.path.join(fake_data_dir, "fake.bam")
    ref_path = os.path.join(fake_data_dir, "systematic_fake_ref.fa")

    # Run VCF SNP
    rc, stdout, stderr = run_cli(
        [
            "vcf",
            "snp",
            "--input",
            bam_path,
            "--ref",
            ref_path,
            "--outdir",
            fake_data_dir,
            "--ploidy",
            "GRCh38",
        ]
    )
    assert rc == 0, f"VCF SNP failed. STDOUT: {stdout}\nSTDERR: {stderr}"
    assert os.path.exists(os.path.join(fake_data_dir, "snps.vcf.gz"))

    # Run VCF Indel
    rc, stdout, stderr = run_cli(
        [
            "vcf",
            "indel",
            "--input",
            bam_path,
            "--ref",
            ref_path,
            "--outdir",
            fake_data_dir,
            "--ploidy",
            "GRCh38",
        ]
    )
    assert rc == 0, f"VCF Indel failed. STDOUT: {stdout}\nSTDERR: {stderr}"
    assert os.path.exists(os.path.join(fake_data_dir, "indels.vcf.gz"))


def test_microarray_generation(fake_data_dir):
    """Test 'microarray' command on generated BAM."""
    bam_path = os.path.join(fake_data_dir, "fake.bam")
    ref_path = os.path.join(fake_data_dir, "systematic_fake_ref.fa")
    snp_tab = os.path.join(fake_data_dir, "dummy_snps.tab.gz")

    rc, stdout, stderr = run_cli(
        [
            "microarray",
            "--input",
            bam_path,
            "--ref",
            ref_path,
            "--ref-vcf-tab",
            snp_tab,
            "--outdir",
            fake_data_dir,
            "--formats",
            "23andme_v5",
        ]
    )
    # Microarray might fail if it can't find templates, but let's see if the VCF part works
    assert rc == 0 or "Failed to generate 23andme_v5" in stderr, (
        f"STDOUT: {stdout}\nSTDERR: {stderr}"
    )

    # CombinedKit.txt is always produced if variant calling worked
    assert os.path.exists(os.path.join(fake_data_dir, "fake_CombinedKit.txt")), (
        f"CombinedKit.txt not found. STDOUT: {stdout}\nSTDERR: {stderr}"
    )


def test_qc_vcf(fake_data_dir):
    """Test 'qc vcf' on generated VCF."""
    vcf_path = os.path.join(fake_data_dir, "fake.vcf.gz")
    rc, stdout, stderr = run_cli(
        ["qc", "vcf", "--input", vcf_path, "--outdir", fake_data_dir]
    )
    assert rc == 0, f"QC VCF failed. STDOUT: {stdout}\nSTDERR: {stderr}"
    assert os.path.exists(os.path.join(fake_data_dir, "fake.vcf.gz.vcfstats.txt"))
