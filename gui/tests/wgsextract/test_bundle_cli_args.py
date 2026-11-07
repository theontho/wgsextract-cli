"""Checks that every wgsextract command the WGSExtract bundle can run parses.

Each page action, row action, data source and setup step is rendered with
representative values and run through the bundle's real POSIX wrapper scripts
against a fake Pixi that records the final `pixi run wgsextract ...` command
line. Those command lines are then parsed with the CLI's own argparse parser,
so a flag the CLI dropped or renamed fails here instead of in the app.

The parser check needs the wgsextract CLI: the runtime installed into the
bundle by setup, or a `wgsextract` console script on PATH (for example the one
in a wgsextract-cli checkout's `.pixi/envs/default/bin`). Without either it is
skipped. Run with `python -m unittest discover -s tests/wgsextract`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE = REPO_ROOT / "examples" / "WGSExtract"
sys.path.insert(0, str(REPO_ROOT / "tools"))

from json_comments import loads_json_with_comments

PLACEHOLDER = re.compile(r"\{\{([^}]+)\}\}")

# Scripts that end in a `wgsextract` invocation, and scripts that never do.
CLI_SCRIPTS = {
    "bootstrap-reference-library.sh",
    "library-state.sh",
    "run-wgsextract-vcf.sh",
    "run-wgsextract.sh",
    "unalign-to-fastq.sh",
}
NON_CLI_SCRIPTS = {
    "bootstrap-wgsextract-config.sh",
    "check-preinstalled-pixi.sh",
    "check-xcode-command-line-tools.sh",
    "delete-reference-genome.sh",
    "list-reference-genomes.py",
    "setup-wgsextract-pixi.sh",
    "test-genome-library.py",
    "uninstall-wgsextract.sh",
}

FAKE_PIXI = """#!/bin/sh
for arg in "$@"; do printf '%s\\0' "$arg"; done > "$(dirname "$0")/../calls"
"""

# Runs inside the CLI's interpreter: reads argv lists on stdin and prints, per
# argv, null when it parses to a runnable command, else the problem: the
# argparse error, or an option the CLI documents as deprecated.
PARSE_CHECK = """
import argparse, contextlib, io, json, sys
from wgsextract_cli.cli.parser import build_parser, parse_args

sys.argv[0] = "wgsextract"
parser, base_parser = build_parser()


def subparsers(command):
    for action in command._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def deprecated_options(argv):
    command = parser
    for token in argv:
        command = subparsers(command).get(token, command)
    return [
        token.split("=", 1)[0]
        for action in command._actions
        if "deprecated" in (action.help or "").lower()
        for token in argv
        if token.split("=", 1)[0] in action.option_strings
    ]


results = []
for argv in json.load(sys.stdin):
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            args, _ = parse_args(parser, base_parser, argv)
    except SystemExit as error:
        lines = stderr.getvalue().strip().splitlines()
        results.append(lines[-1] if lines else f"argparse exited {error.code}")
        continue
    if not hasattr(args, "func"):
        results.append("not a runnable subcommand")
    elif deprecated := deprecated_options(argv):
        results.append(f"uses deprecated option(s) {', '.join(deprecated)}")
    else:
        results.append(None)
json.dump(results, sys.stdout)
"""


def find_cli_python() -> Path | None:
    """Return an interpreter that can import wgsextract_cli, if any."""
    candidates = [BUNDLE / "runtime/wgsextract-cli/app/.pixi/envs/default/bin/python"]
    console_script = shutil.which("wgsextract")
    if console_script:
        first_line = Path(console_script).read_bytes().split(b"\n", 1)[0]
        if first_line.startswith(b"#!") and b"python" in first_line:
            candidates.append(Path(first_line[2:].decode().split()[0]))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        probe = subprocess.run(
            [str(candidate), "-c", "import wgsextract_cli.cli.parser"],
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    return None


class BundleSandbox:
    """A copy of the bundle scripts with a fake installed runtime."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bundle = root / "bundle"
        shutil.copytree(BUNDLE / "scripts", self.bundle / "scripts")
        runtime = self.bundle / "runtime" / "wgsextract-cli"
        (runtime / "app").mkdir(parents=True)
        pixi = runtime / ".pixi" / "bin" / "pixi"
        pixi.parent.mkdir(parents=True)
        pixi.write_text(FAKE_PIXI)
        pixi.chmod(0o755)
        self.calls = runtime / ".pixi" / "calls"
        self.values = self._representative_values()

    def _file(self, relative: str) -> str:
        path = self.root / "data" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return str(path)

    def _representative_values(self) -> dict[str, str]:
        data = self.root / "data"
        fasta = self._file("reference/genomes/hs38.fa.gz")
        bam = self._file("genomes/sample/sample.bam")
        vcf = self._file("genomes/sample/sample.vcf.gz")
        r1 = self._file("genomes/sample/sample_R1.fastq.gz")
        r2 = self._file("genomes/sample/sample_R2.fastq.gz")
        return {
            "bundleRoot": str(self.bundle),
            "bundleWorkspace": str(self.bundle),
            "bam_path": bam,
            "realign_bam": bam,
            "ref_path": str(data / "reference"),
            "ref_fasta": fasta,
            "realign_ref": fasta,
            "row.ref": fasta,
            "row.code": "hs38",
            "row.final": "hs38.fa.gz",
            "out_dir": str(data / "output"),
            "genome_library": str(data / "genomes"),
            "vep_cache_path": str(data / "vep"),
            "vcf_path": vcf,
            "vcf_child": vcf,
            "vcf_mother": vcf,
            "vcf_father": vcf,
            "vcf_ann_vcf": vcf,
            "vep_input_vcf": vcf,
            "library.annotationVcfArgument": vcf,
            "library.spliceaiFile": self._file("reference/spliceai.vcf.gz"),
            "library.alphamissenseFile": self._file("reference/alphamissense.tsv.gz"),
            "library.pharmgkbFile": self._file("reference/pharmgkb.tsv"),
            "fastq_r1": r1,
            "fastq_r2": r2,
            "pet_fastq_r1": r1,
            "pet_fastq_r2": r2,
            "yleaf_path": self._file("tools/yleaf"),
            "yleaf_pos": self._file("tools/yleaf_positions.txt"),
            "haplogrep_path": self._file("tools/haplogrep"),
            "vcf_region": "chrM:1-16569",
            "extract_region": "chrM",
            "vcf_gene": "BRCA1",
            "vcf_filter_expr": "QUAL>30",
            # A single dash-leading word must not be parsed as an option.
            "vcf_vep_args": "--everything",
            "extract_extra": "0.25",
            "cram_version": "3.0",
            "fastq_output_format": "BAM",
            "pet_output_format": "CRAM",
            "pet_species": "dog",
            "microarray_formats": "23andme_v5,ancestry_v2",
            "microarray_min_gq": "20",
            "microarray_min_dp": "5",
            "microarray_strand_ambiguous": "no-call",
            "microarray_resolve_rsids": "--resolve-rsids",
            "microarray_absent_no_call": "--no-assume-hom-ref",
            "microarray_exclude_filtered": "--exclude-filtered",
            "microarray_call_reasons": "--call-reasons",
            "aadr_sample_id": "research_sample",
            "aadr_sex": "U",
            "indel_format": "myheritage_gsa_2025",
            "indel_manifest": self._file("manifests/matching chip.csv"),
            "extract_poz": "--poz",
            "extract_ybrowse": "--ybrowse",
            "extract_unmapped_fastq": "--fastq",
            "lineage_tree_links": "--tree-links",
            "realign_keep_fastq": "--keep-fastq",
            "gatk_scatter": "--scatter",
            "multiqc_input": self._file("reports/sample_fastqc.zip"),
            "multiqc_directory": str(data / "reports"),
            "multiqc_force": "--force",
            "deepvariant_model": "ONT_R104",
            "sv_read_type": "--long-read",
            "sv_tandem_repeats": self._file("reference/repeats.bed"),
            "clair3_model": "r1041_e82_400bps_sup_v500",
            "methylation_context": "cpg",
            "methylation_5hmc": "--5hmc",
            "methylation_combine_strands": "--no-combine-strands",
            "y_str_panel": "Y-37",
            "vcf_table_sample": "sample",
            "vcf_table_all_alleles": "--all-alleles",
            "vcf_table_no_sqlite": "--no-sqlite",
            "report_last_failed": "--last-failed",
            "report_description": "It stopped",
        }

    def render(self, value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1).strip()
            if name not in self.values:
                raise KeyError(f"no representative value for {{{{{name}}}}}")
            return self.values[name]

        return PLACEHOLDER.sub(replace, value)

    def run_cli_script(self, script: str, args: list[str]) -> list[str]:
        """Run a POSIX wrapper and return the argv it passed to wgsextract."""
        self.calls.unlink(missing_ok=True)
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root),
            "GUI_FOR_CLI_BUNDLE_WORKSPACE": str(self.bundle),
        }
        result = subprocess.run(
            ["/bin/sh", str(self.bundle / "scripts" / "posix" / script), *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=self.bundle,
            check=False,
        )
        if result.returncode != 0 or not self.calls.is_file():
            raise AssertionError(
                f"{script} exited {result.returncode} without running Pixi:\n{result.stderr}"
            )
        argv = self.calls.read_bytes().split(b"\0")[:-1]
        call = [arg.decode() for arg in argv]
        if call[:2] != ["run", "wgsextract"]:
            raise AssertionError(
                f"{script} ran `pixi {' '.join(call)}`, not wgsextract"
            )
        # The runtime must use the workspace's settings, never the user's config.
        config = ["--config", str(self.bundle / "settings" / "config.toml")]
        if call[2:4] != config:
            raise AssertionError(f"{script} did not pass {' '.join(config)}: {call}")
        return call[4:]


def load_json(path: Path) -> dict:
    return loads_json_with_comments(path.read_text(encoding="utf-8"))


def script_name(path: str) -> str:
    return path.replace("{{bundleRoot}}/", "").removeprefix("scripts/")


def bundle_commands() -> Iterator[tuple[str, str, list[str]]]:
    """Yield (label, script, arguments) for everything the bundle can run.

    Optional argument groups are always included, so every flag is checked.
    """
    manifest = load_json(BUNDLE / "manifest.json")
    for phase in ("setup", "uninstall"):
        for step in manifest.get(phase, {}).get("steps", []):
            if step["kind"] == "setupScript":
                yield (
                    f"{phase}:{step['id']}",
                    script_name(step["value"]),
                    step.get("arguments", []),
                )
    for page_file in manifest["pages"]:
        page = load_json(BUNDLE / "pages" / page_file)
        for section in page.get("sections", []):
            label = f"{page['id']}/{section['id']}"
            if source := section.get("dataSource"):
                yield (
                    f"{label}:dataSource",
                    script_name(source["path"]),
                    source.get("arguments", []),
                )
            actions = list(section.get("actions", []))
            for control in section.get("controls", []):
                if source := control.get("dataSource"):
                    yield (
                        f"{label}/{control['id']}:dataSource",
                        script_name(source["path"]),
                        source.get("arguments", []),
                    )
                for setting in control.get("settings", []):
                    if source := setting.get("dataSource"):
                        yield (
                            f"{label}/{setting['id']}:dataSource",
                            script_name(source["path"]),
                            source.get("arguments", []),
                        )
                actions += control.get("rowActions", [])
            for action in actions:
                command = action["command"]
                arguments = command.get("arguments", []) + [
                    argument
                    for group in command.get("optionalArguments", [])
                    for argument in group
                ]
                yield (
                    f"{label}:{action['id']}",
                    script_name(command["executable"]),
                    arguments,
                )


class BundleCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="wgsextract-bundle-")
        self.addCleanup(temp.cleanup)
        self.sandbox = BundleSandbox(Path(temp.name))

    def cli_invocations(self) -> list[tuple[str, list[str]]]:
        invocations = []
        for label, script, arguments in bundle_commands():
            with self.subTest(command=label):
                self.assertIn(
                    script,
                    CLI_SCRIPTS | NON_CLI_SCRIPTS,
                    f"classify {script} in CLI_SCRIPTS or NON_CLI_SCRIPTS",
                )
                rendered = [self.sandbox.render(argument) for argument in arguments]
                if script in CLI_SCRIPTS:
                    invocations.append(
                        (label, self.sandbox.run_cli_script(script, rendered))
                    )
        return invocations

    def test_bundle_commands_render_and_reach_wgsextract(self) -> None:
        self.assertGreater(len(self.cli_invocations()), 40)

    def test_bundle_commands_parse_with_current_cli(self) -> None:
        python = find_cli_python()
        if python is None:
            self.skipTest(
                "wgsextract CLI not found: run WGSExtract setup or put a wgsextract-cli "
                "checkout's .pixi/envs/default/bin on PATH"
            )
        invocations = self.cli_invocations()
        result = subprocess.run(
            [str(python), "-c", PARSE_CHECK],
            input=json.dumps(
                [["--config", "settings.toml", *argv] for _, argv in invocations]
            ),
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(self.sandbox.root)},
            check=True,
        )
        for (label, argv), error in zip(
            invocations, json.loads(result.stdout), strict=True
        ):
            with self.subTest(command=label):
                self.assertIsNone(error, f"wgsextract {' '.join(argv)}")

    def test_library_state_forwards_annotation_inputs(self) -> None:
        argv = self.sandbox.run_cli_script(
            "library-state.sh", ["/ref", "/genomes", "/ann.vcf.gz", "/in.vcf.gz"]
        )
        self.assertEqual(
            argv,
            [
                "ref",
                "status",
                "--values",
                "--ref",
                "/ref",
                "--genome-library",
                "/genomes",
                "--annotation-vcf",
                "/ann.vcf.gz",
                "--input",
                "/in.vcf.gz",
            ],
        )

    def test_vcf_wrapper_adds_ploidy_for_reference_fasta(self) -> None:
        bam = self.sandbox.values["bam_path"]
        fasta = self.sandbox.values["ref_fasta"]
        library = self.sandbox.values["ref_path"]
        self.assertEqual(
            self.sandbox.run_cli_script(
                "run-wgsextract-vcf.sh", ["snp", "--input", bam, "--ref", fasta]
            ),
            ["vcf", "snp", "--input", bam, "--ref", fasta, "--ploidy", "GRCh38"],
        )
        # A library directory is left to the CLI, which finds its ploidy file.
        self.assertEqual(
            self.sandbox.run_cli_script(
                "run-wgsextract-vcf.sh", ["indel", "--input", bam, "--ref", library]
            ),
            ["vcf", "indel", "--input", bam, "--ref", library],
        )
        self.assertEqual(
            self.sandbox.run_cli_script(
                "run-wgsextract-vcf.sh", ["snp", "--ref", fasta, "--ploidy", "1"]
            ),
            ["vcf", "snp", "--ref", fasta, "--ploidy", "1"],
        )


if __name__ == "__main__":
    unittest.main()
