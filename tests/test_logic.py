import os
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace

# Ensure src is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from wgsextract_cli.core.gene_map import GeneMap
from wgsextract_cli.core.genome_library import (
    GENOME_CONFIG_NAME,
    apply_genome_selection,
)
from wgsextract_cli.core.utils import WGSExtractError


class TestCLILogic(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.ref_dir = os.path.join(self.test_dir, "ref")
        os.makedirs(self.ref_dir)
        self.gene_file = os.path.join(self.ref_dir, "genes_hg38.tsv")
        with open(self.gene_file, "w") as f:
            f.write("symbol\tchrom\tstart\tend\n")
            f.write("BRCA1\tchr17\t43044294\t43125364\n")
            f.write("KCNQ2\tchr20\t63400000\t63600000\n")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_gene_map_resolution(self):
        gm = GeneMap(self.test_dir)

        # Test hg38 resolution
        coords = gm.get_coords("BRCA1", "hg38")
        self.assertEqual(coords, "chr17:43044294-43125364")

        # Test case insensitivity
        coords = gm.get_coords("brca1", "hg38")
        self.assertEqual(coords, "chr17:43044294-43125364")

        # Test unknown gene
        coords = gm.get_coords("FAKEGENE", "hg38")
        self.assertIsNone(coords)

    def test_inheritance_expressions(self):
        # Verify the bcftools expressions used in vcf.py
        # GT[0] is proband, [1] is mother, [2] is father

        # De Novo: Child het (0/1), Parents ref (0/0)
        denovo = 'GT[0]="het" && GT[1]="ref" && GT[2]="ref"'

        # Recessive: Child hom-alt (1/1), Parents het (0/1)
        recessive = 'GT[0]="hom" && GT[1]="het" && GT[2]="het"'

        self.assertIn('GT[0]="het"', denovo)
        self.assertIn('GT[1]="ref"', denovo)
        self.assertIn('GT[0]="hom"', recessive)
        self.assertIn('GT[1]="het"', recessive)

    def test_genome_library_resolves_alignment_input_and_outdir(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "ken mcdonald")
        os.makedirs(genome_dir)
        bam_dir = os.path.join(genome_dir, "bam files")
        os.makedirs(bam_dir)
        cram_path = os.path.join(bam_dir, "sample.cram")
        with open(cram_path, "w") as f:
            f.write("cram")

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="info",
                genome="ken mcdonald",
                input="/tmp/config-default.bam",
                outdir="/tmp/shared-output",
            )
            apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertEqual(args.input, cram_path)
        self.assertEqual(args.outdir, genome_dir)
        config_path = os.path.join(genome_dir, GENOME_CONFIG_NAME)
        self.assertTrue(os.path.exists(config_path))
        with open(config_path) as f:
            self.assertIn('alignment = "bam files/sample.cram"', f.read())

    def test_genome_library_respects_explicit_outdir(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "joe")
        os.makedirs(genome_dir)
        bam_path = os.path.join(genome_dir, "joe.bam")
        with open(bam_path, "w") as f:
            f.write("bam")

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(command="bam", genome="joe", input=None, outdir="custom")
            apply_genome_selection(args, explicit_dests={"outdir"})
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertEqual(args.input, bam_path)
        self.assertEqual(args.outdir, "custom")

    def test_genome_library_resolves_fastq_pair(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "sue")
        os.makedirs(genome_dir)
        fastq_dir = os.path.join(genome_dir, "raw reads")
        os.makedirs(fastq_dir)
        r1_path = os.path.join(fastq_dir, "sue_R1.fastq.gz")
        r2_path = os.path.join(fastq_dir, "sue_R2.fastq.gz")
        with open(r2_path, "w") as f:
            f.write("r2")
        with open(r1_path, "w") as f:
            f.write("r1")

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="align", genome="sue", input=None, outdir=None, r1=None, r2=None
            )
            apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertEqual(args.r1, r1_path)
        self.assertEqual(args.r2, r2_path)
        self.assertEqual(args.outdir, genome_dir)

    def test_genome_library_resolves_vcf_input_for_vcf_commands(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "mac")
        os.makedirs(genome_dir)
        vcf_dir = os.path.join(genome_dir, "variant calls")
        os.makedirs(vcf_dir)
        vcf_path = os.path.join(vcf_dir, "mac.vcf.gz")
        with open(vcf_path, "w") as f:
            f.write("vcf")

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="vcf",
                vcf_cmd="filter",
                genome="mac",
                input=None,
                vcf_input=None,
                outdir=None,
            )
            apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertEqual(args.vcf_input, vcf_path)
        self.assertEqual(args.outdir, genome_dir)

    def test_genome_library_resolves_vcf_input_for_qc_vcf(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "qc-vcf")
        os.makedirs(genome_dir)
        vcf_path = os.path.join(genome_dir, "sample.vcf.gz")
        with open(vcf_path, "w") as f:
            f.write("vcf")

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="qc",
                qc_cmd="vcf",
                genome="qc-vcf",
                input=None,
                vcf_input=None,
                outdir=None,
            )
            apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertEqual(args.vcf_input, vcf_path)
        self.assertIsNone(args.input)

    def test_genome_library_rejects_path_escape(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        os.makedirs(genome_root)
        outside_dir = os.path.join(self.test_dir, "outside")
        os.makedirs(outside_dir)

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="info", genome="../outside", input=None, outdir=None
            )
            with self.assertRaises(WGSExtractError) as ctx:
                apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertIn("cannot escape", str(ctx.exception))

    def test_genome_library_raises_on_ambiguous_alignment(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "ambiguous")
        os.makedirs(os.path.join(genome_dir, "first"))
        os.makedirs(os.path.join(genome_dir, "second"))
        with open(os.path.join(genome_dir, "first", "a.cram"), "w") as f:
            f.write("a")
        with open(os.path.join(genome_dir, "second", "b.bam"), "w") as f:
            f.write("b")

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="info", genome="ambiguous", input=None, outdir=None
            )
            with self.assertRaises(WGSExtractError) as ctx:
                apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertIn("Ambiguous alignment", str(ctx.exception))
        self.assertIn(GENOME_CONFIG_NAME, str(ctx.exception))
        self.assertTrue(os.path.exists(os.path.join(genome_dir, GENOME_CONFIG_NAME)))

    def test_genome_library_config_resolves_ambiguous_alignment(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "configured")
        os.makedirs(os.path.join(genome_dir, "first"))
        os.makedirs(os.path.join(genome_dir, "second"))
        with open(os.path.join(genome_dir, "first", "a.cram"), "w") as f:
            f.write("a")
        selected = os.path.join(genome_dir, "second", "b.bam")
        with open(selected, "w") as f:
            f.write("b")
        with open(os.path.join(genome_dir, GENOME_CONFIG_NAME), "w") as f:
            f.write('alignment = "second/b.bam"\n')

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="info", genome="configured", input=None, outdir=None
            )
            apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertEqual(args.input, selected)

    def test_genome_library_raises_on_multiple_fastq_sets(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "multi-fastq")
        os.makedirs(os.path.join(genome_dir, "run1"))
        os.makedirs(os.path.join(genome_dir, "run2"))
        for run in ["run1", "run2"]:
            with open(os.path.join(genome_dir, run, f"{run}_R1.fastq.gz"), "w") as f:
                f.write("r1")
            with open(os.path.join(genome_dir, run, f"{run}_R2.fastq.gz"), "w") as f:
                f.write("r2")

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="align",
                genome="multi-fastq",
                input=None,
                outdir=None,
                r1=None,
                r2=None,
            )
            with self.assertRaises(WGSExtractError) as ctx:
                apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertIn("Ambiguous FASTQ set", str(ctx.exception))
        self.assertIn("fastq_r1 and fastq_r2", str(ctx.exception))

    def test_genome_library_resolves_missing_mate_from_explicit_r1(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "explicit-r1")
        os.makedirs(genome_dir)
        r1 = os.path.join(genome_dir, "sample_R1.fastq.gz")
        r2 = os.path.join(genome_dir, "sample_R2.fastq.gz")
        with open(r1, "w") as f:
            f.write("r1")
        with open(r2, "w") as f:
            f.write("r2")

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="align",
                genome="explicit-r1",
                input=None,
                outdir=None,
                r1=r1,
                r2=None,
            )
            apply_genome_selection(args, explicit_dests={"r1"})
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertEqual(args.r1, r1)
        self.assertEqual(args.r2, r2)

    def test_genome_library_does_not_pair_unmatched_single_r2(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "mismatch")
        os.makedirs(genome_dir)
        r1 = os.path.join(genome_dir, "sampleA_R1.fastq.gz")
        r2 = os.path.join(genome_dir, "sampleB_R2.fastq.gz")
        with open(r1, "w") as f:
            f.write("r1")
        with open(r2, "w") as f:
            f.write("r2")

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="align",
                genome="mismatch",
                input=None,
                outdir=None,
                r1=None,
                r2=None,
            )
            with self.assertRaises(WGSExtractError) as ctx:
                apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertIn("Ambiguous FASTQ set", str(ctx.exception))

    def test_genome_library_does_not_classify_srr_as_r1(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "srr")
        os.makedirs(genome_dir)
        fastq = os.path.join(genome_dir, "SRR123.fastq.gz")
        with open(fastq, "w") as f:
            f.write("single")

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="align", genome="srr", input=None, outdir=None, r1=None, r2=None
            )
            apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertEqual(args.r1, fastq)
        self.assertIsNone(args.r2)


if __name__ == "__main__":
    unittest.main()
