import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.variant_files import calculate_bam_md5
from wgsextract_cli.main import main


@pytest.mark.real_config
class TestMicroarrayRealData:
    """
    End-to-end test for microarray command using real genomic data.
    Verifies genotype accuracy against source BAM/CRAM for multiple vendors.

    Inputs: --real-input/--real-ref, else the input_path/reference_library
    settings; --full-data runs the whole genome instead of chr1.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, pytestconfig: pytest.Config, tmp_path: Path) -> None:
        input_path = pytestconfig.getoption("real_input") or settings.get("input_path")
        ref_path = pytestconfig.getoption("real_ref") or settings.get(
            "reference_library"
        )
        if not (input_path and ref_path and os.path.exists(input_path)):
            pytest.skip(
                f"Real data not configured or missing: REF={ref_path}, INPUT={input_path}"
            )
        self.ref_path = ref_path
        self.full_data = pytestconfig.getoption("full_data")
        self.test_dir = str(tmp_path)

        # Symlink the input so indexes and outputs land in the test dir.
        ext = os.path.splitext(input_path)[1]
        self.isolated_input = os.path.join(self.test_dir, f"input_isolated{ext}")
        os.symlink(input_path, self.isolated_input)

        md5_sig = calculate_bam_md5(self.isolated_input)
        self.ref_fasta = ReferenceLibrary(ref_path, md5_sig).fasta
        if not self.ref_fasta or not os.path.exists(self.ref_fasta):
            pytest.skip(f"Reference FASTA not found: {self.ref_fasta}")

        subprocess.run(["samtools", "index", self.isolated_input], check=True)

    def run_samtools_mpileup(self, chrom, pos):
        """Helper to get base at position from source BAM/CRAM."""
        if not chrom.startswith("chr"):
            chrom = f"chr{chrom}"
        region = f"{chrom}:{pos}-{pos}"
        cmd = [
            "samtools",
            "mpileup",
            "-r",
            region,
            "-f",
            self.ref_fasta,
            self.isolated_input,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not res.stdout.strip():
            return None

        parts = res.stdout.strip().splitlines()[0].split("\t")
        if len(parts) < 5:
            return None
        ref_base, bases_str = parts[2].upper(), parts[4]
        called_bases = []
        i = 0
        while i < len(bases_str):
            b = bases_str[i]
            if b in ".,":
                called_bases.append(ref_base)
            elif b in "ACGTNacgt":
                called_bases.append(b.upper())
            elif b == "^":
                i += 1
            elif b in "+-":
                num_str = ""
                i += 1
                while i < len(bases_str) and bases_str[i].isdigit():
                    num_str += bases_str[i]
                    i += 1
                i += int(num_str) - 1
            i += 1
        return "".join(sorted(set(called_bases))) if called_bases else None

    def test_microarray_multi_vendor(self):
        """
        Runs microarray command for multiple vendors and verifies results.
        """
        # 1. Run CLI for 23andMe, Ancestry, and MyHeritage
        formats_to_test = ["23andme_v3", "ancestry_v2", "myheritage_v2"]
        args = [
            "--outdir",
            self.test_dir,
            "--ref",
            self.ref_path,
            "--input",
            self.isolated_input,
            "microarray",
            "--formats",
            ",".join(formats_to_test),
        ]

        if not self.full_data:
            args.extend(["--region", "chr1"])

        print("\n>>> Running multi-vendor microarray on real data...")
        try:
            main(args)
        except SystemExit as e:
            assert e.code in (0, None)

        output_prefix = os.path.basename(self.isolated_input).split(".")[0]

        # 2. Verify Base CombinedKit.txt (GEDMATCH/All)
        base_txt = os.path.join(self.test_dir, f"{output_prefix}_CombinedKit.txt")
        assert os.path.exists(base_txt), "Base CombinedKit.txt missing"
        with open(base_txt) as f:
            base_lines = [line for line in f if not line.startswith("#")]

        print(f"Base CombinedKit.txt generated with {len(base_lines)} SNPs")
        assert len(base_lines) > 1000

        # 3. Verify each vendor format
        verified_snps = {
            "rs3094315": {"hg19": "752566", "hg38": "817186"},
            "rs3131972": {"hg19": "752721", "hg38": "817341"},
        }

        vendor_files = {
            "23andme_v3": f"{output_prefix}_23andMe_V3.txt",
            "ancestry_v2": f"{output_prefix}_Ancestry_V2.txt",
            "myheritage_v2": f"{output_prefix}_MyHeritage_V2.csv",
        }

        for fmt_key, filename in vendor_files.items():
            zip_path = os.path.join(
                self.test_dir, filename.replace(".txt", ".zip").replace(".csv", ".zip")
            )
            out_path = os.path.join(self.test_dir, filename)
            if os.path.exists(zip_path):
                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    zip_ref.extractall(self.test_dir)

            assert os.path.exists(out_path), f"Output file missing for {fmt_key}"

            with open(out_path) as f:
                lines = f.readlines()

            data_lines = [
                line.strip()
                for line in lines
                if not line.startswith("#") and line.strip()
            ]
            print(f"Format {fmt_key}: {len(data_lines)} SNPs")
            assert len(data_lines) > 10000

            # Check specific SNPs in this vendor file
            delimiter = "," if filename.endswith(".csv") else "\t"
            for line in data_lines:
                # Remove quotes for CSV formats like MyHeritage
                parts = line.replace('"', "").split(delimiter)
                if len(parts) < 4:
                    continue

                snp_id = parts[0]
                if snp_id in verified_snps:
                    v = verified_snps[snp_id]
                    # Genotype is the last field (or last two for Ancestry)
                    genotype = "".join(parts[3:]).replace("\t", "").replace(" ", "")

                    source_geno = self.run_samtools_mpileup("chr1", v["hg38"])
                    if source_geno:
                        print(
                            f"[{fmt_key}] SNP {snp_id}: Output={genotype}, Source={source_geno}"
                        )
                        for b in genotype:
                            if b not in "-0":  # Skip uncalled
                                assert b in source_geno

        print("<<< Multi-vendor verification complete.")
