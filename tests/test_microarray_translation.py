import os
import shutil

# Ensure src is in sys.path
import sys
import tempfile
import unittest
from pathlib import Path

cli_src = Path(__file__).resolve().parent.parent / "src"
if str(cli_src) not in sys.path:
    sys.path.insert(0, str(cli_src))

from wgsextract_cli.core.microarray_utils import (  # noqa: E402
    convert_to_vendor_format,
    liftover_hg38_to_hg19,
)
from wgsextract_cli.core.utils import WGSExtractError  # noqa: E402


class TestMicroarrayTranslation(unittest.TestCase):
    """
    Focused test for the translation logic from hg38 to hg19.
    Verifies coordinate shifts and chromosome normalization.
    """

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="wgse_trans_test_")

        # We need a real chain file for pyliftover to work.
        # We'll try to find it in the environment or workspace.
        self.chain_file = os.environ.get("WGSE_REFERENCE_FASTA", "")
        if self.chain_file:
            self.chain_file = os.path.join(self.chain_file, "hg38ToHg19.over.chain.gz")

        if not os.path.exists(self.chain_file):
            # Fallback to standard workspace location
            repo_root = Path(__file__).resolve().parent.parent
            self.chain_file = str(repo_root / "reference" / "hg38ToHg19.over.chain.gz")

        # Templates dir for vendor formatting
        self.templates_dir = os.environ.get("WGSE_REFERENCE_FASTA", "")
        if self.templates_dir:
            if not os.path.exists(
                os.path.join(self.templates_dir, "microarray", "raw_file_templates")
            ):
                if not os.path.exists(
                    os.path.join(self.templates_dir, "raw_file_templates")
                ):
                    repo_root = Path(__file__).resolve().parent.parent
                    self.templates_dir = str(repo_root / "reference" / "microarray")
        else:
            repo_root = Path(__file__).resolve().parent.parent
            self.templates_dir = str(repo_root / "reference" / "microarray")

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir)

    def test_hg38_to_hg19_coordinate_shift(self) -> None:
        """
        Verify that specific hg38 coordinates are shifted to correct hg19 positions.
        """
        if not os.path.exists(self.chain_file):
            self.skipTest(
                f"Liftover chain file not found at {self.chain_file}. Skipping translation verification."
            )

        # Create a mock hg38 CombinedKit.txt
        # Format: ID, CHROM, POS, RESULT
        # rs3094315: hg38=817186 -> hg19=752566
        # rs12124819: hg38=841166 -> hg19=776546
        input_txt = os.path.join(self.test_dir, "input_hg38.txt")
        with open(input_txt, "w") as f:
            f.write("rs3094315\t1\t817186\tAG\n")
            f.write("rs12124819\t1\t841166\tAA\n")
            f.write("mt_test\tMT\t100\tCC\n")

        output_txt = os.path.join(self.test_dir, "output_hg19.txt")

        # Run liftover
        liftover_hg38_to_hg19(input_txt, output_txt, self.chain_file)

        # Verify results
        with open(output_txt) as f:
            lines = [line.strip().split("\t") for line in f if not line.startswith("#")]

        results = {line[0]: line for line in lines}

        self.assertIn("rs3094315", results)
        self.assertEqual(
            results["rs3094315"][2], "752566", "hg19 coordinate mismatch for rs3094315"
        )
        self.assertEqual(
            results["rs3094315"][1], "1", "Chromosome normalization failed"
        )

        self.assertIn("rs12124819", results)
        self.assertEqual(
            results["rs12124819"][2],
            "776546",
            "hg19 coordinate mismatch for rs12124819",
        )

        self.assertIn("mt_test", results)
        self.assertEqual(
            results["mt_test"][1], "MT", "Mito chromosome normalization failed"
        )

    def test_vendor_formatting_preservation(self) -> None:
        """
        Verify that genotypes and RSIDs are preserved when converting to vendor format.
        """
        # Templates root resolution (matching microarray_utils logic)
        templates_root = self.templates_dir
        if os.path.isdir(
            os.path.join(templates_root, "microarray", "raw_file_templates")
        ):
            templates_root = os.path.join(templates_root, "microarray")

        if not os.path.exists(os.path.join(templates_root, "raw_file_templates")):
            self.skipTest(
                f"Microarray templates not found in {templates_root}. Skipping vendor format check."
            )

        # Create a mock hg19 CombinedKit.txt (what liftover produces)
        input_hg19 = os.path.join(self.test_dir, "kit_hg19.txt")
        with open(input_hg19, "w") as f:
            f.write("rs3094315\t1\t752566\tAG\n")
            f.write("rs3131972\t1\t752721\tGG\n")

        out_23andme = os.path.join(self.test_dir, "23andme_v3.txt")

        # Run formatting
        convert_to_vendor_format("23andMe_V3", input_hg19, out_23andme, templates_root)

        # Verify content
        with open(out_23andme) as f:
            lines = f.readlines()

        data = [line.strip().split("\t") for line in lines if not line.startswith("#")]
        results = {line[0]: line for line in data}

        self.assertIn("rs3094315", results)
        self.assertEqual(
            results["rs3094315"][3], "AG", "Genotype lost in vendor conversion"
        )
        self.assertEqual(
            results["rs3094315"][2], "752566", "Position mismatch in vendor output"
        )

        self.assertIn("rs3131972", results)
        self.assertEqual(
            results["rs3131972"][3], "GG", "Genotype lost in vendor conversion"
        )

    def test_vendor_formatting_finds_templates_from_reference_genomes_dir(self) -> None:
        """Ensure vendor templates are found when the FASTA lives under genomes/."""
        ref_root = os.path.join(self.test_dir, "reference")
        genomes_dir = os.path.join(ref_root, "genomes")
        templates_root = os.path.join(ref_root, "microarray", "raw_file_templates")
        os.makedirs(genomes_dir, exist_ok=True)
        os.makedirs(os.path.join(templates_root, "head"), exist_ok=True)
        os.makedirs(os.path.join(templates_root, "body"), exist_ok=True)

        with open(os.path.join(templates_root, "head", "23andMe_V5.txt"), "w") as f:
            f.write("# rsid\tchromosome\tposition\tgenotype\n")
        with open(os.path.join(templates_root, "body", "23andMe_V5_1.txt"), "w") as f:
            f.write("rs3094315\t1\t752566\n")
        with open(os.path.join(templates_root, "body", "23andMe_V5_2.txt"), "w") as f:
            f.write("rs3131972\t1\t752721\n")

        input_hg19 = os.path.join(self.test_dir, "kit_hg19.txt")
        with open(input_hg19, "w") as f:
            f.write("rs3094315\t1\t752566\tAG\n")
            f.write("rs3131972\t1\t752721\tGG\n")

        out_23andme = os.path.join(self.test_dir, "23andme_v5.txt")

        convert_to_vendor_format(
            "23andMe_V5",
            input_hg19,
            out_23andme,
            genomes_dir,
        )

        with open(out_23andme) as f:
            lines = [line.strip() for line in f if not line.startswith("#")]

        self.assertEqual(len(lines), 2)
        self.assertIn("rs3094315\t1\t752566\tAG", lines)
        self.assertIn("rs3131972\t1\t752721\tGG", lines)

    def test_vendor_formatting_errors_when_templates_are_missing(self) -> None:
        """When templates_dir is empty/None or missing, fail without output."""
        input_hg19 = os.path.join(self.test_dir, "kit_hg19.txt")
        with open(input_hg19, "w") as f:
            f.write("rs3094315\t1\t752566\tAG\n")

        for i, templates_dir in enumerate(
            (None, "", os.path.join(self.test_dir, "nonexistent"))
        ):
            out_path = os.path.join(self.test_dir, f"out_{i}.txt")
            with self.assertRaisesRegex(
                WGSExtractError, "Microarray templates not found"
            ):
                convert_to_vendor_format(
                    "23andMe_V5",
                    input_hg19,
                    out_path,
                    templates_dir,
                )
            self.assertFalse(os.path.exists(out_path))

    def test_vendor_formatting_errors_when_body_template_is_missing(self) -> None:
        """A partial template set should fail instead of creating empty parts."""
        templates_root = os.path.join(self.test_dir, "raw_file_templates")
        os.makedirs(os.path.join(templates_root, "head"), exist_ok=True)
        os.makedirs(os.path.join(templates_root, "body"), exist_ok=True)

        input_hg19 = os.path.join(self.test_dir, "kit_hg19.txt")
        with open(input_hg19, "w") as f:
            f.write("rs3094315\t1\t752566\tAG\n")

        out_path = os.path.join(self.test_dir, "out_missing_body.txt")
        with self.assertRaisesRegex(WGSExtractError, "Template body not found"):
            convert_to_vendor_format("23andMe_V5", input_hg19, out_path, self.test_dir)

        self.assertFalse(os.path.exists(out_path))

    def test_partial_template_root_does_not_mask_complete_later_root(self) -> None:
        """A partial nearby template tree should not hide a complete later root."""
        partial_root = os.path.join(self.test_dir, "near", "raw_file_templates")
        complete_root = os.path.join(self.test_dir, "far", "raw_file_templates")
        os.makedirs(os.path.join(partial_root, "head"), exist_ok=True)
        os.makedirs(os.path.join(partial_root, "body"), exist_ok=True)
        os.makedirs(os.path.join(complete_root, "head"), exist_ok=True)
        os.makedirs(os.path.join(complete_root, "body"), exist_ok=True)

        with open(os.path.join(complete_root, "head", "23andMe_V5.txt"), "w") as f:
            f.write("# rsid\tchromosome\tposition\tgenotype\n")
        with open(os.path.join(complete_root, "body", "23andMe_V5_1.txt"), "w") as f:
            f.write("rs3094315\t1\t752566\n")
        with open(os.path.join(complete_root, "body", "23andMe_V5_2.txt"), "w") as f:
            f.write("rs3131972\t1\t752721\n")

        input_hg19 = os.path.join(self.test_dir, "kit_hg19.txt")
        with open(input_hg19, "w") as f:
            f.write("rs3094315\t1\t752566\tAG\n")
            f.write("rs3131972\t1\t752721\tGG\n")

        out_path = os.path.join(self.test_dir, "out_v5.txt")
        convert_to_vendor_format(
            "23andMe_V5",
            input_hg19,
            out_path,
            [os.path.dirname(partial_root), os.path.dirname(complete_root)],
        )

        with open(out_path) as f:
            lines = [line.strip() for line in f if not line.startswith("#")]

        self.assertEqual(
            lines,
            [
                "rs3094315\t1\t752566\tAG",
                "rs3131972\t1\t752721\tGG",
            ],
        )


if __name__ == "__main__":
    unittest.main()
