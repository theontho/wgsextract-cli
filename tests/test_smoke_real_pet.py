import glob
import os

import pytest

from tests.smoke_utils import check_tool, run_cli, verify_bam, verify_vcf
from wgsextract_cli.core.config import settings


@pytest.mark.skipif(
    not settings.get("pet_r1_fastq")
    or not settings.get("pet_r2_fastq")
    or not settings.get("pet_reference_fasta"),
    reason="pet_r1_fastq/r2_fastq/reference_fasta settings not set",
)
class TestPetAlignSmoke:
    """Ported from test_pet_align_full.sh"""

    @pytest.fixture(autouse=True)
    def setup_pet(self, tmp_path):
        self.outdir = str(tmp_path)
        self.r1 = settings.get("pet_r1_fastq")
        self.r2 = settings.get("pet_r2_fastq")
        self.ref = settings.get("pet_reference_fasta")

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
