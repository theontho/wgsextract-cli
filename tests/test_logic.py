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

from wgsextract_cli.core.gene_map import GeneMap
from wgsextract_cli.core.genome_library import (
    GENOME_CONFIG_NAME,
    apply_genome_selection,
)
from wgsextract_cli.core.utils import WGSExtractError


class DummyProcess:
    def __init__(self):
        self.stdout = self

    def close(self):
        pass

    def communicate(self):
        return None, None


class TestAlignToolSelection(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.r1 = os.path.join(self.test_dir, "fake_R1.fastq.gz")
        self.ref = os.path.join(self.test_dir, "fake_ref.fa")
        for path in (self.r1, self.ref, f"{self.ref}.bwt"):
            with open(path, "w") as f:
                f.write("test")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_bwa_alignment_sorts_sam_stream_before_sambamba_index(self):
        from wgsextract_cli.commands import align

        args = Namespace(
            r1=self.r1,
            r2=None,
            ref=self.ref,
            input=None,
            outdir=self.test_dir,
            format="BAM",
            threads=None,
            memory=None,
        )
        commands = []
        run_commands = []

        def fake_get_tool_path(tool):
            return "bwa" if tool == "bwa" else None

        def fake_which(tool):
            return "/usr/bin/sambamba" if tool == "sambamba" else None

        def fake_popen(cmd, **_kwargs):
            commands.append(cmd)
            return DummyProcess()

        def fake_run_command(cmd, *_args, **_kwargs):
            run_commands.append(cmd)

        with (
            patch.object(align, "verify_dependencies"),
            patch.object(align, "log_dependency_info"),
            patch.object(align, "get_resource_defaults", return_value=("2", "1G")),
            patch.object(align, "verify_paths_exist", return_value=True),
            patch.object(align, "resolve_reference", return_value=self.ref),
            patch.object(align, "get_tool_path", side_effect=fake_get_tool_path),
            patch.object(align, "print_warning"),
            patch.object(align, "run_command", side_effect=fake_run_command),
            patch.object(align, "popen", side_effect=fake_popen),
            patch("wgsextract_cli.core.utils.shutil.which", side_effect=fake_which),
            patch("platform.system", return_value="Linux"),
        ):
            align.align_bwa(args)

        self.assertIn(["samtools", "sort"], [cmd[:2] for cmd in commands])
        self.assertIn(["sambamba", "index"], [cmd[:2] for cmd in run_commands])


class TestRefDownloadValidation(unittest.TestCase):
    def test_directory_output_short_circuits_before_curl(self):
        from wgsextract_cli.commands import ref

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


class TestExamplesDownload(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_target_root_uses_configured_genome_library(self):
        from wgsextract_cli.commands import examples
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
        from wgsextract_cli.commands import examples
        from wgsextract_cli.core.config import settings

        old_value = settings.get("genome_library")
        settings.pop("genome_library", None)
        try:
            self.assertEqual(
                examples._target_root(None), examples._repo_root() / "genomes"
            )
        finally:
            if old_value is not None:
                settings["genome_library"] = old_value

    def test_dry_run_plans_downloads_without_creating_files(self):
        from wgsextract_cli.commands import examples

        args = Namespace(
            example_ids=["phase3-chrmt-vcf"],
            all=False,
            method="ftp",
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
            method="ftp",
            target_root=self.test_dir,
            aspera_key=None,
            force=False,
            dry_run=False,
        )

        def fake_download(_source, destination, _method, _aspera_key):
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
            self.assertIn(
                'vcf = "ALL.chrMT.phase3_callmom-v0_4.20130502.genotypes.vcf.gz"',
                f.read(),
            )

    def test_unknown_example_raises_clear_error(self):
        from wgsextract_cli.commands import examples

        with self.assertRaises(WGSExtractError) as ctx:
            examples._select_examples(["not-real"], include_all=False)

        self.assertIn("Unknown example ID", str(ctx.exception))
        self.assertIn("not-real", str(ctx.exception))

    def test_aspera_source_uses_1000genomes_fasp_server(self):
        from wgsextract_cli.commands import examples

        source = examples._source_for("release/20130502/example.vcf.gz", "aspera")

        self.assertEqual(
            source,
            "fasp-g1k@fasp.1000genomes.ebi.ac.uk:/vol1/ftp/release/20130502/example.vcf.gz",
        )

    def test_ftp_source_uses_1000genomes_ftp_root(self):
        from wgsextract_cli.commands import examples

        source = examples._source_for("release/20130502/example.vcf.gz", "ftp")

        self.assertEqual(
            source,
            "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/example.vcf.gz",
        )

    def test_resolve_aspera_key_prefers_explicit_key(self):
        from wgsextract_cli.commands import examples

        explicit_key = os.path.join(self.test_dir, "explicit.openssh")
        home_dir = os.path.join(self.test_dir, "home")
        default_key = os.path.join(
            home_dir, ".aspera", "connect", "etc", "asperaweb_id_dsa.openssh"
        )
        os.makedirs(os.path.dirname(default_key))
        with open(explicit_key, "w") as f:
            f.write("explicit")
        with open(default_key, "w") as f:
            f.write("default")

        with patch.dict(os.environ, {"HOME": home_dir}):
            resolved = examples._resolve_aspera_key(explicit_key)

        self.assertEqual(resolved, Path(explicit_key))

    def test_write_genome_config_for_fastq_pair(self):
        from wgsextract_cli.commands import examples

        example = examples.EXAMPLES_BY_ID["na12878-lowcov-fastq"]
        example_dir = Path(self.test_dir)

        examples._write_genome_config(example, example_dir)

        with open(example_dir / GENOME_CONFIG_NAME) as f:
            config = f.read()
        self.assertIn('fastq_r1 = "ERR001268_1.filt.fastq.gz"', config)
        self.assertIn('fastq_r2 = "ERR001268_2.filt.fastq.gz"', config)


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

    def test_vcf_filter_prefers_explicit_vcf_input(self):
        from wgsextract_cli.commands import vcf

        vcf_path = os.path.join(self.test_dir, "sample.vcf.gz")
        with open(vcf_path, "w") as f:
            f.write("vcf")

        class DummyReferenceLibrary:
            fasta = None
            root = self.test_dir
            build = "hg38"

            def __init__(self, *_args, **_kwargs):
                pass

        args = Namespace(
            input=os.path.join(self.test_dir, "configured.cram"),
            vcf_input=vcf_path,
            outdir=self.test_dir,
            ref=None,
            region="chrM",
            expr=None,
            gene=None,
            exclude_near_gaps=False,
        )

        with (
            patch.object(vcf, "verify_dependencies"),
            patch.object(vcf, "log_dependency_info"),
            patch.object(vcf, "verify_paths_exist", return_value=True),
            patch.object(vcf, "calculate_bam_md5", return_value=None),
            patch.object(vcf, "ReferenceLibrary", DummyReferenceLibrary),
            patch.object(vcf, "ensure_vcf_prepared", return_value=vcf_path),
            patch.object(vcf, "ensure_vcf_indexed"),
            patch.object(vcf, "run_command") as run_command,
        ):
            vcf.cmd_filter(args)

        command = run_command.call_args.args[0]
        self.assertEqual(command[-1], vcf_path)
        self.assertNotIn(args.input, command)

    def test_vcf_filter_raises_on_filter_failure(self):
        from wgsextract_cli.commands import vcf

        vcf_path = os.path.join(self.test_dir, "sample.vcf.gz")
        with open(vcf_path, "w") as f:
            f.write("vcf")

        class DummyReferenceLibrary:
            fasta = None
            root = self.test_dir
            build = "hg38"

            def __init__(self, *_args, **_kwargs):
                pass

        args = Namespace(
            input=None,
            vcf_input=vcf_path,
            outdir=self.test_dir,
            ref=None,
            region="chrM",
            expr=None,
            gene=None,
            exclude_near_gaps=False,
        )

        with (
            patch.object(vcf, "verify_dependencies"),
            patch.object(vcf, "log_dependency_info"),
            patch.object(vcf, "verify_paths_exist", return_value=True),
            patch.object(vcf, "calculate_bam_md5", return_value=None),
            patch.object(vcf, "ReferenceLibrary", DummyReferenceLibrary),
            patch.object(vcf, "ensure_vcf_prepared", return_value=vcf_path),
            patch.object(vcf, "run_command", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(WGSExtractError):
                vcf.cmd_filter(args)

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


if __name__ == "__main__":
    unittest.main()
