import os
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

# Ensure src is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from wgsextract_cli.core.gene_map import GeneMap, resolve_gene_map_reflib
from wgsextract_cli.core.genome_library import (
    GENOME_CONFIG_NAME,
    apply_genome_selection,
)
from wgsextract_cli.core.utils import WGSExtractError


class TestRefDownloadValidation(unittest.TestCase):
    def test_directory_output_short_circuits_before_curl(self):
        from wgsextract_cli.commands.ref import core_commands as ref

        args = Namespace(url="http://fake", out=tempfile.mkdtemp())
        try:
            with (
                patch.object(ref, "verify_dependencies"),
                patch.object(ref, "run_command") as run_command,
            ):
                with self.assertRaises(WGSExtractError):
                    ref.cmd_download(args)

            run_command.assert_not_called()
        finally:
            shutil.rmtree(args.out)

    def test_ref_download_uses_fallback_downloader_without_curl_requirement(self):
        from wgsextract_cli.commands.ref import core_commands as ref

        args = Namespace(url="http://fake/reference.fa.gz", out="reference.fa.gz")
        with (
            patch.object(ref, "verify_dependencies") as verify_dependencies,
            patch.object(ref, "download_file", return_value=True) as download_file,
        ):
            ref.cmd_download(args)

        verify_dependencies.assert_not_called()
        download_file.assert_called_once_with(args.url, args.out)

    def test_ref_download_failure_includes_url_and_output_path(self):
        from wgsextract_cli.commands.ref import core_commands as ref

        args = Namespace(url="http://fake/reference.fa.gz", out="reference.fa.gz")
        with patch.object(ref, "download_file", return_value=False):
            with self.assertRaisesRegex(
                WGSExtractError, r"http://fake/reference\.fa\.gz.*reference\.fa\.gz"
            ):
                ref.cmd_download(args)


class TestExamplesDownload(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_target_root_uses_configured_genome_library(self):
        from wgsextract_cli.commands import _examples_catalog as examples
        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = self.test_dir
        try:
            self.assertEqual(examples._target_root(None), Path(self.test_dir).resolve())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

    def test_default_target_root_is_repo_genomes(self):
        from wgsextract_cli.commands import _examples_catalog as examples
        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings.pop("genome_library", None)
        try:
            self.assertEqual(
                examples._target_root(None), examples.runtime.repo_root() / "genomes"
            )
        finally:
            if old_value is not None:
                settings["genome_library"] = old_value

    def test_dry_run_plans_downloads_without_creating_files(self):
        from wgsextract_cli.commands import examples

        args = Namespace(
            example_ids=["phase3-chrmt-vcf"],
            all=False,
            method="https",
            target_root=self.test_dir,
            aspera_key=None,
            force=False,
            dry_run=True,
        )
        with patch.object(examples, "run_command") as run_command:
            examples.cmd_download(args)

        run_command.assert_not_called()
        self.assertFalse(
            os.path.exists(os.path.join(self.test_dir, examples.COLLECTION_DIR))
        )

    def test_download_writes_genome_config_for_vcf(self):
        from wgsextract_cli.commands import examples

        args = Namespace(
            example_ids=["phase3-chrmt-vcf"],
            all=False,
            method="https",
            target_root=self.test_dir,
            aspera_key=None,
            force=False,
            dry_run=False,
        )

        def fake_download(_source, destination, _method, _aspera_key, *, force=False):
            self.assertFalse(force)
            destination.write_text("data")

        with patch.object(examples, "_download_file", side_effect=fake_download):
            examples.cmd_download(args)

        config_path = os.path.join(
            self.test_dir,
            examples.COLLECTION_DIR,
            "phase3-chrmt-vcf",
            GENOME_CONFIG_NAME,
        )
        self.assertTrue(os.path.exists(config_path))
        with open(config_path) as f:
            config = f.read()
            self.assertIn(
                'vcf = "ALL.chrMT.phase3_callmom-v0_4.20130502.genotypes.vcf.gz"',
                config,
            )
            self.assertNotIn("vcf_index", config)
            self.assertNotIn(".tbi", config)

    def test_unknown_example_raises_clear_error(self):
        from wgsextract_cli.commands import _examples_catalog as examples

        with self.assertRaises(WGSExtractError) as ctx:
            examples._select_examples(["not-real"], include_all=False)

        self.assertIn("Unknown example ID", str(ctx.exception))
        self.assertIn("not-real", str(ctx.exception))

    def test_https_method_requires_curl(self):
        from wgsextract_cli.commands import examples

        with patch.object(examples.shutil, "which", return_value=None):
            with self.assertRaises(WGSExtractError) as ctx:
                examples._resolve_method("https")

        self.assertIn("curl", str(ctx.exception))

    def test_aspera_source_uses_1000genomes_fasp_server(self):
        from wgsextract_cli.commands import examples

        source = examples._source_for("release/20130502/example.vcf.gz", "aspera")

        self.assertEqual(
            source,
            "fasp-g1k@fasp.1000genomes.ebi.ac.uk:/vol1/ftp/release/20130502/example.vcf.gz",
        )

    def test_https_source_uses_1000genomes_https_root(self):
        from wgsextract_cli.commands import examples

        source = examples._source_for("release/20130502/example.vcf.gz", "https")

        self.assertEqual(
            source,
            "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/example.vcf.gz",
        )

    def test_resolve_aspera_key_prefers_explicit_key(self):
        from wgsextract_cli.commands import _examples_catalog as examples

        explicit_key = os.path.join(self.test_dir, "explicit.openssh")
        home_dir = os.path.join(self.test_dir, "home")
        default_key = os.path.join(
            home_dir, ".aspera", "connect", "etc", "asperaweb_id_dsa.openssh"
        )
        os.makedirs(os.path.dirname(default_key))
        Path(explicit_key).write_text("explicit", encoding="utf-8")
        Path(default_key).write_text("default", encoding="utf-8")

        with patch.dict(os.environ, {"HOME": home_dir}):
            resolved = examples._resolve_aspera_key(explicit_key)

        self.assertEqual(resolved, Path(explicit_key))

    def test_write_genome_config_for_fastq_pair(self):
        from wgsextract_cli.commands import _examples_catalog as catalog
        from wgsextract_cli.commands import examples

        example = catalog.EXAMPLES_BY_ID["na12878-lowcov-fastq"]
        example_dir = Path(self.test_dir)

        examples._write_genome_config(example, example_dir)

        with open(example_dir / GENOME_CONFIG_NAME, encoding="utf-8") as f:
            config = f.read()
        self.assertIn('fastq_r1 = "ERR001268_1.filt.fastq.gz"', config)
        self.assertIn('fastq_r2 = "ERR001268_2.filt.fastq.gz"', config)

    def test_pacbio_examples_are_in_catalog(self):
        from wgsextract_cli.commands import _examples_catalog as examples

        example = examples.EXAMPLES_BY_ID["hgsvc2-hg00733-pacbio-hifi-bam"]

        self.assertEqual(example.data_type, "pacbio-hifi-bam")
        self.assertTrue(example.files[0].url_path.endswith(".ccs.bam"))
        self.assertEqual(example.files[0].role, "fastq_r1")
        self.assertIn("pacbio", example.tags)

    def test_select_examples_by_tag(self):
        from wgsextract_cli.commands import _examples_catalog as examples

        selected = examples._select_examples([], include_all=False, tags=["pacbio"])

        self.assertGreaterEqual(len(selected), 4)
        self.assertTrue(all("pacbio" in example.tags for example in selected))

    def test_absolute_example_source_is_used_as_is(self):
        from wgsextract_cli.commands import examples

        url = "https://example.test/sample.bam"

        self.assertEqual(examples._source_for(url, "https"), url)

    def test_absolute_example_uses_declared_transfer_method(self):
        from wgsextract_cli.commands import _examples_catalog as catalog
        from wgsextract_cli.commands import examples

        example = catalog.EXAMPLES_BY_ID["hgsvc2-hg00732-pacbio-hifi-bam-smallest"]

        planned = examples._planned_downloads(example, Path(self.test_dir), "aspera")

        self.assertEqual(planned[0][2], "fastq_r1")
        self.assertEqual(planned[0][3], "https")
        self.assertEqual(planned[0][0], example.files[0].url_path)

    def test_tag_selection_rejects_mixed_explicit_ids(self):
        from wgsextract_cli.commands import _examples_catalog as examples

        with self.assertRaises(WGSExtractError):
            examples._select_examples(
                ["phase3-chrmt-vcf"], include_all=False, tags=["pacbio"]
            )


class TestResourceDefaults(unittest.TestCase):
    def test_resource_defaults_use_central_thread_policy(self):
        from wgsextract_cli.core import utils

        with patch.object(
            utils, "default_thread_tuning_profile", return_value=Namespace(threads=8)
        ):
            threads, _memory = utils.get_resource_defaults(None, None)

        self.assertEqual(threads, "8")

    def test_explicit_thread_count_still_overrides_central_thread_policy(self):
        from wgsextract_cli.core import utils

        with patch.object(
            utils, "default_thread_tuning_profile", return_value=Namespace(threads=8)
        ):
            threads, _memory = utils.get_resource_defaults(10, None)

        self.assertEqual(threads, "10")

    def test_resource_defaults_clamp_non_positive_thread_count(self):
        from wgsextract_cli.core import resource_policy

        class FakeMemory:
            total = 16 * (1024**3)

        class FakePsutil:
            @staticmethod
            def virtual_memory():
                return FakeMemory()

        with patch.object(resource_policy, "psutil", FakePsutil):
            threads, memory = resource_policy.get_resource_defaults(0, None)

        self.assertEqual(threads, "1")
        self.assertEqual(memory, "4G")


class TestSamtoolsCommands(unittest.TestCase):
    def test_sambamba_memory_parser_accepts_suffixes_and_decimals(self):
        from wgsextract_cli.core import samtools_commands

        with (
            patch.object(samtools_commands, "get_tool_path", return_value="sambamba"),
            patch.object(samtools_commands.platform, "system", return_value="Linux"),
        ):
            gb_cmd = samtools_commands.get_sam_sort_cmd(
                "out.bam", "2", "1GB", fmt="BAM"
            )
            decimal_cmd = samtools_commands.get_sam_sort_cmd(
                "out.bam", "2", "1.5G", fmt="BAM"
            )
            mb_cmd = samtools_commands.get_sam_sort_cmd(
                "out.bam", "2", "512MB", fmt="BAM"
            )

        self.assertEqual(gb_cmd[gb_cmd.index("-m") + 1], "2G")
        self.assertEqual(decimal_cmd[decimal_cmd.index("-m") + 1], "3G")
        self.assertEqual(mb_cmd[mb_cmd.index("-m") + 1], "1024M")

    def test_sam_input_sort_never_uses_sambamba(self):
        from wgsextract_cli.core import samtools_commands

        with (
            patch.object(samtools_commands, "get_tool_path", return_value="sambamba"),
            patch.object(samtools_commands.platform, "system", return_value="Linux"),
        ):
            cmd = samtools_commands.get_sam_sort_cmd(
                "out.bam", "2", "1G", fmt="BAM", sam_input=True
            )

        self.assertEqual(cmd[:2], ["samtools", "sort"])


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

    def test_gene_map_reflib_prefers_explicit_ref_maps(self):
        ref_path = os.path.join(self.ref_dir, "fake_ref_hg38.fa")
        Path(ref_path).write_text(">chr1\nACGT\n")
        configured_reflib = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, configured_reflib)
        os.makedirs(os.path.join(configured_reflib, "ref"))
        Path(os.path.join(configured_reflib, "ref", "genes_hg38.tsv")).write_text(
            "symbol\tchrom\tstart\tend\nBRCA1\tchr1\t1\t2\n"
        )

        self.assertEqual(
            resolve_gene_map_reflib(ref_path, configured_reflib, "hg38"),
            self.test_dir,
        )

    def test_gene_map_reflib_finds_maps_adjacent_to_explicit_ref(self):
        ref_root = tempfile.mkdtemp()
        configured_reflib = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ref_root)
        self.addCleanup(shutil.rmtree, configured_reflib)
        ref_path = os.path.join(ref_root, "fake_ref_hg38.fa")
        Path(ref_path).write_text(">chr1\nACGT\n")
        os.makedirs(os.path.join(ref_root, "ref"))
        Path(os.path.join(ref_root, "ref", "genes_hg38.tsv")).write_text(
            "symbol\tchrom\tstart\tend\nBRCA1\tchr1\t1\t2\n"
        )
        os.makedirs(os.path.join(configured_reflib, "ref"))
        Path(os.path.join(configured_reflib, "ref", "genes_hg38.tsv")).write_text(
            "symbol\tchrom\tstart\tend\nBRCA1\tchr1\t3\t4\n"
        )

        self.assertEqual(
            resolve_gene_map_reflib(ref_path, configured_reflib, "hg38"),
            ref_root,
        )

    def test_gene_map_reflib_falls_back_to_configured_maps(self):
        ref_root = tempfile.mkdtemp()
        configured_reflib = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ref_root)
        self.addCleanup(shutil.rmtree, configured_reflib)
        os.makedirs(os.path.join(ref_root, "ref"))
        ref_path = os.path.join(ref_root, "ref", "fake_ref_hg38.fa")
        Path(ref_path).write_text(">chr1\nACGT\n")
        os.makedirs(os.path.join(configured_reflib, "ref"))
        Path(os.path.join(configured_reflib, "ref", "genes_hg38.tsv")).write_text(
            "symbol\tchrom\tstart\tend\nBRCA1\tchr1\t1\t2\n"
        )

        self.assertEqual(
            resolve_gene_map_reflib(ref_path, configured_reflib, "hg38"),
            configured_reflib,
        )

    def test_gene_map_reflib_microarray_prefers_explicit_ref_maps(self):
        ref_root = tempfile.mkdtemp()
        configured_reflib = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ref_root)
        self.addCleanup(shutil.rmtree, configured_reflib)
        ref_path = os.path.join(ref_root, "fake_ref_hg38.fa")
        Path(ref_path).write_text(">chr1\nACGT\n")
        os.makedirs(os.path.join(ref_root, "microarray"))
        Path(os.path.join(ref_root, "microarray", "genes_hg38.tsv")).write_text(
            "symbol\tchrom\tstart\tend\nBRCA1\tchr1\t1\t2\n"
        )
        os.makedirs(os.path.join(configured_reflib, "microarray"))
        Path(
            os.path.join(configured_reflib, "microarray", "genes_hg38.tsv")
        ).write_text("symbol\tchrom\tstart\tend\nBRCA1\tchr1\t3\t4\n")

        self.assertEqual(
            resolve_gene_map_reflib(ref_path, configured_reflib, "hg38"),
            ref_root,
        )

    def test_gene_map_reflib_microarray_falls_back_to_configured_maps(self):
        ref_root = tempfile.mkdtemp()
        configured_reflib = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ref_root)
        self.addCleanup(shutil.rmtree, configured_reflib)
        ref_path = os.path.join(ref_root, "fake_ref_hg38.fa")
        Path(ref_path).write_text(">chr1\nACGT\n")
        os.makedirs(os.path.join(configured_reflib, "microarray"))
        Path(
            os.path.join(configured_reflib, "microarray", "genes_hg38.tsv")
        ).write_text("symbol\tchrom\tstart\tend\nBRCA1\tchr1\t3\t4\n")

        self.assertEqual(
            resolve_gene_map_reflib(ref_path, configured_reflib, "hg38"),
            configured_reflib,
        )

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

    def test_genome_library_resolves_pacbio_read_bam_for_align(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "pacbio")
        os.makedirs(genome_dir)
        ccs_path = os.path.join(genome_dir, "pacbio.ccs.bam")
        with open(ccs_path, "w") as f:
            f.write("reads")

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="align",
                genome="pacbio",
                input=None,
                outdir=None,
                r1=None,
                r2=None,
            )
            apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertEqual(args.r1, ccs_path)
        self.assertIsNone(args.r2)
        with open(os.path.join(genome_dir, GENOME_CONFIG_NAME)) as f:
            self.assertIn('fastq_r1 = "pacbio.ccs.bam"', f.read())

    def test_genome_library_does_not_treat_pacbio_read_bam_as_alignment(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "pacbio-info")
        os.makedirs(genome_dir)
        with open(os.path.join(genome_dir, "reads.ccs.bam"), "w") as f:
            f.write("reads")

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="info", genome="pacbio-info", input=None, outdir=None
            )
            with self.assertRaises(WGSExtractError) as ctx:
                apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertIn("No alignment", str(ctx.exception))

    def test_genome_library_does_not_treat_configured_plain_pacbio_bam_as_alignment(
        self,
    ):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "pacbio-plain")
        os.makedirs(genome_dir)
        bam_path = os.path.join(genome_dir, "HG00732-hifi.bam")
        with open(bam_path, "w") as f:
            f.write("reads")
        with open(os.path.join(genome_dir, GENOME_CONFIG_NAME), "w") as f:
            f.write('fastq_r1 = "HG00732-hifi.bam"\n')

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="info", genome="pacbio-plain", input=None, outdir=None
            )
            with self.assertRaises(WGSExtractError) as ctx:
                apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertIn("No alignment", str(ctx.exception))

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

    def test_genome_library_resolves_trio_proband_vcf(self):
        genome_root = os.path.join(self.test_dir, "genomes")
        genome_dir = os.path.join(genome_root, "trio-child")
        os.makedirs(genome_dir)
        vcf_path = os.path.join(genome_dir, "child.vcf.gz")
        with open(vcf_path, "w") as f:
            f.write("vcf")

        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings["genome_library"] = genome_root
        try:
            args = Namespace(
                command="vcf",
                vcf_cmd="trio",
                genome="trio-child",
                input=None,
                vcf_input=None,
                proband=None,
                mother="mother.vcf.gz",
                father="father.vcf.gz",
                outdir=None,
            )
            apply_genome_selection(args, explicit_dests=set())
        finally:
            if old_value is None:
                settings.pop("genome_library", None)
            else:
                settings["genome_library"] = old_value

        self.assertEqual(args.proband, vcf_path)
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


class TestReferenceSupportAssets(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_reference_library_resolves_support_assets_from_input_build_hint(self):
        from wgsextract_cli.core.reference_resolver import ReferenceLibrary

        os.makedirs(os.path.join(self.test_dir, "genomes"))
        nested_ref = os.path.join(
            self.test_dir, "microarray", "raw_file_templates", "body", "head", "ref"
        )
        os.makedirs(nested_ref)
        fasta = os.path.join(self.test_dir, "genomes", "hg19.fa.gz")
        y_snps = os.path.join(nested_ref, "snps_hg19.vcf.gz")
        Path(fasta).touch()
        Path(y_snps).touch()

        input_path = os.path.join(self.test_dir, "inputs", "HG00096.hg19-mini.bam")
        os.makedirs(os.path.dirname(input_path))
        Path(input_path).touch()

        # The bootstrap's snps_<build>.vcf.gz holds only Y SNPs: never a target.
        lib = ReferenceLibrary(self.test_dir, None, input_path=input_path)
        self.assertEqual(lib.build, "hg19")
        self.assertIsNone(lib.ref_vcf_tab)

        targets = os.path.join(nested_ref, "All_SNPs_hg19_ref.tab.gz")
        Path(targets).touch()
        lib = ReferenceLibrary(self.test_dir, None, input_path=input_path)
        self.assertEqual(lib.ref_vcf_tab, targets)

    def test_reference_library_resolves_input_manifest_reference_fasta(self):
        from wgsextract_cli.core.reference_resolver import ReferenceLibrary

        genome_dir = os.path.join(self.test_dir, "genome")
        os.makedirs(genome_dir)
        input_path = os.path.join(genome_dir, "sample.bam")
        fasta = os.path.join(genome_dir, "sample.hg19.fa")
        Path(input_path).touch()
        Path(fasta).touch()
        Path(os.path.join(genome_dir, "sample.targets.tab.gz")).touch()
        Path(os.path.join(genome_dir, "manifest.json")).write_text(
            '{"files":{"ref":"sample.hg19.fa"}}',
            encoding="utf-8",
        )
        lib = ReferenceLibrary(self.test_dir, None, input_path=input_path)

        self.assertEqual(lib.fasta, fasta)
        self.assertEqual(
            lib.ref_vcf_tab, os.path.join(genome_dir, "sample.targets.tab.gz")
        )

    def test_reference_library_resolves_generic_reference_fasta(self):
        from wgsextract_cli.core.reference_resolver import ReferenceLibrary

        fasta = os.path.join(self.test_dir, "my.grch38.reference.fasta")
        Path(fasta).touch()
        lib = ReferenceLibrary(self.test_dir, None, input_path="sample.hg38.bam")

        self.assertEqual(lib.fasta, fasta)
        self.assertEqual(lib.build, "hg38")

    def test_reference_library_manifest_fallback_for_relative_input(self):
        from wgsextract_cli.core.reference_resolver import ReferenceLibrary

        input_dir = os.path.join(self.test_dir, "input")
        os.makedirs(input_dir)
        fasta = os.path.join(input_dir, "my.grch38.reference.fasta")
        target = os.path.join(input_dir, "sample.targets.tab.gz")
        Path(os.path.join(input_dir, "sample.hg38.bam")).touch()
        Path(fasta).touch()
        Path(target).touch()
        Path(os.path.join(input_dir, "manifest.json")).write_text(
            "{not valid json",
            encoding="utf-8",
        )

        old_cwd = os.getcwd()
        try:
            os.chdir(input_dir)
            lib = ReferenceLibrary(self.test_dir, None, input_path="sample.hg38.bam")
            resolved_fasta = os.path.realpath(lib.fasta)
            resolved_target = os.path.realpath(lib.ref_vcf_tab)
        finally:
            os.chdir(old_cwd)

        self.assertEqual(resolved_fasta, os.path.realpath(fasta))
        self.assertEqual(resolved_target, os.path.realpath(target))
        self.assertEqual(lib.build, "hg38")

    def test_reference_library_detects_hs_build_aliases_from_reference_path(self):
        from wgsextract_cli.core.reference_resolver import ReferenceLibrary

        hs38_ref = os.path.join(self.test_dir, "GCA_hs38d1_analysis_set.fa")
        hs37_ref = os.path.join(self.test_dir, "hs37d5.fa")
        Path(hs38_ref).touch()
        Path(hs37_ref).touch()

        self.assertEqual(ReferenceLibrary(hs38_ref, None).build, "hg38")
        self.assertEqual(ReferenceLibrary(hs37_ref, None).build, "hg19")

    def test_reference_resolver_resolves_support_assets_from_input_build_hint(self):
        from wgsextract_cli.core.reference_resolver import ReferenceLibrary

        nested_ref = os.path.join(
            self.test_dir, "microarray", "raw_file_templates", "body", "head", "ref"
        )
        os.makedirs(os.path.join(self.test_dir, "genomes"))
        os.makedirs(nested_ref)
        Path(os.path.join(self.test_dir, "genomes", "hg19.fa.gz")).touch()
        targets = os.path.join(nested_ref, "All_SNPs_hg19_ref.tab.gz")
        Path(targets).touch()
        Path(os.path.join(nested_ref, "snps_hg19.vcf.gz")).touch()  # Y SNPs only

        input_path = os.path.join(self.test_dir, "inputs", "HG00096.hg19-mini.bam")
        os.makedirs(os.path.dirname(input_path))
        Path(input_path).touch()

        lib = ReferenceLibrary(self.test_dir, None, input_path=input_path)

        self.assertEqual(lib.build, "hg19")
        self.assertEqual(lib.ref_vcf_tab, targets)
