import glob
import os

import pytest

from tests.smoke_utils import check_tool, run_cli, verify_bam, verify_vcf
from wgsextract_cli.core.config import settings


@pytest.mark.skipif(
    not (os.environ.get("WGSE_PET_R1") or settings.get("pet_r1_fastq"))
    or not (os.environ.get("WGSE_PET_R2") or settings.get("pet_r2_fastq"))
    or not (os.environ.get("WGSE_PET_REF") or settings.get("pet_reference_fasta")),
    reason="pet FASTQ/reference settings or WGSE_PET_* variables not set",
)
class TestPetAlignSmoke:
    """Ported from test_pet_align_full.sh"""

    @pytest.fixture(autouse=True)
    def setup_pet(self, tmp_path):
        self.outdir = str(tmp_path)
        self.r1 = os.environ.get("WGSE_PET_R1") or settings.get("pet_r1_fastq")
        self.r2 = os.environ.get("WGSE_PET_R2") or settings.get("pet_r2_fastq")
        self.ref = os.environ.get("WGSE_PET_REF") or settings.get("pet_reference_fasta")
        self.species = os.environ.get("WGSE_PET_SPECIES", "dog")

    @pytest.mark.skipif(
        not check_tool("bwa") and not check_tool("bwa-mem2"),
        reason="bwa or bwa-mem2 missing",
    )
    def test_pet_align_full(self):
        args = [
            "pet-align",
            "--r1",
            self.r1,
            "--r2",
            self.r2,
            "--ref",
            self.ref,
            "--outdir",
            self.outdir,
            "--species",
            self.species,
        ]
        rc, stdout, stderr = run_cli(args)
        assert rc == 0

        # Check for BAM/CRAM output
        align_files = glob.glob(os.path.join(self.outdir, "*.bam")) + glob.glob(
            os.path.join(self.outdir, "*.cram")
        )
        assert len(align_files) > 0
        assert verify_bam(align_files[0])

        # Check for VCF output
        vcf_files = glob.glob(os.path.join(self.outdir, "*.vcf.gz"))
        assert len(vcf_files) > 0
        assert verify_vcf(vcf_files[0])
