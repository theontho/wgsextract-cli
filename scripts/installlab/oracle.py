"""Judge an install-lab run from the evidence the guest left behind.

The oracle runs on the host, with the host's own samtools/bcftools as measuring
instruments. It fails closed: a required step without evidence, an output that
is missing or wrong, or any state the uninstaller left behind is a failure.
Expected values come from biology where possible (HG00096's published mtDNA
haplogroup, its 1000 Genomes genotypes), not from a previous run of this code.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# HG00096's mtDNA haplogroup as published by 1000 Genomes / PhyloTree.
HG00096_MT_HAPLOGROUP = "H16a1"

# Steps the scenario must have run, in order. Missing evidence fails the run.
REQUIRED_STEPS = [
    "install",
    "dataset",
    "workload",
    "update",
    "update_kept_data",
    "post_update_mt",
    "uninstall",
]
WORKLOAD_STEPS = [
    "help",
    "deps",
    "bootstrap",
    "info",
    "info_cram",
    "mt_bam",
    "mito_vcf",
    "mt_haplo",
    "y_haplo",
    "snp",
    "microarray",
    "align",
    "gatk",
    "must_fail",
]
# Extra steps a target's scenario must have run (Windows downloads the installer
# as its own step, and the user deletes the downloaded folder after uninstall).
TARGET_STEPS = {
    "windows": ["installer_download", "remove_install_dir"],
    "windows-host": ["installer_download", "remove_install_dir"],
}
# Steps that must fail, and the message that proves they failed for the right reason.
EXPECTED_FAILURES = {
    "must_fail": "not found",
    # The mini genome's chrY slice is rebased, so no hg19 Y markers match.
    "y_haplo": "could not predict a Y haplogroup",
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


class Oracle:
    # A path the install must visibly add, so a clean uninstall diff means something.
    install_marker = "/wgsextract-cli"

    def __init__(self, results: Path, nonce: str, tools: dict[str, str]) -> None:
        self.results = results
        self.nonce = nonce
        self.tools = tools
        self.checks: list[Check] = []
        try:
            meta = json.loads((results / "lab-run.json").read_text())
        except (OSError, ValueError):
            meta = {}
        self.target = str(meta.get("target", ""))

    # --- helpers ------------------------------------------------------------

    def record(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, ok, detail))
        return ok

    def evidence(self, step: str, workload: bool = False) -> Path:
        base = self.results / "workload" if workload else self.results
        return base / "evidence" / step

    def rc(self, step: str, workload: bool = False) -> int | None:
        path = self.evidence(step, workload).with_suffix(".rc")
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            return None

    def log(self, step: str, workload: bool = False) -> str:
        path = self.evidence(step, workload).with_suffix(".log")
        try:
            return path.read_text(errors="replace")
        except OSError:
            return ""

    def run_tool(self, tool: str, *args: str) -> str:
        result = subprocess.run(
            [self.tools[tool], *args], capture_output=True, text=True, check=True
        )
        return result.stdout

    def count_records(self, vcf: Path) -> int:
        return len(self.run_tool("bcftools", "view", "-H", str(vcf)).splitlines())

    def output(self, *parts: str) -> Path:
        return self.results.joinpath("workload", *parts)

    # --- checks -------------------------------------------------------------

    def check_run_identity(self) -> None:
        try:
            nonce = (self.results / "nonce").read_text().strip()
        except OSError:
            nonce = ""
        self.record(
            "evidence.nonce",
            nonce == self.nonce,
            f"expected {self.nonce}, got {nonce!r}",
        )

    def check_gatekeeper_enabled(self) -> None:
        """A macOS lab machine must have Gatekeeper on, like a user's Mac."""
        try:
            status = (self.results / "gatekeeper-status.txt").read_text().strip()
        except OSError:
            status = "no status recorded"
        self.record(
            "baseline.gatekeeper_enabled", status == "assessments enabled", status
        )

    def check_clean_baseline(self) -> None:
        try:
            lines = (self.results / "preexisting-tools.txt").read_text().splitlines()
        except OSError:
            self.record("baseline.no_preinstalled_tools", False, "no tool survey")
            return
        found = [line for line in lines if "\t" in line and line.split("\t", 1)[1]]
        self.record(
            "baseline.no_preinstalled_tools",
            bool(lines) and not found,
            "; ".join(found)
            if found
            else "none of pixi/samtools/bcftools/java/brew/conda",
        )

    def check_steps(self) -> None:
        for step in REQUIRED_STEPS + TARGET_STEPS.get(self.target, []):
            rc = self.rc(step)
            self.record(f"step.{step}", rc == 0, f"exit code {rc}")
        for step in WORKLOAD_STEPS:
            rc = self.rc(step, workload=True)
            if step in EXPECTED_FAILURES:
                message = EXPECTED_FAILURES[step]
                found = message in self.log(step, workload=True)
                self.record(
                    f"workload.{step}",
                    rc not in (None, 0) and found,
                    f"exit code {rc}; message {message!r} {'found' if found else 'missing'}",
                )
            else:
                self.record(f"workload.{step}", rc == 0, f"exit code {rc}")

    def check_outputs(self, dataset: Path) -> None:
        checks = {
            "help": self._help,
            "bootstrap": self._bootstrap,
            "info": self._info,
            "mt_bam": self._mt_bam,
            "mito_vcf": self._mito_vcf,
            "mt_haplogroup": self._mt_haplogroup,
            "snp_recall_vs_1000g": lambda: self._snp_recall(dataset),
            "microarray": lambda: self._microarray(dataset),
            "align": self._align,
            "gatk_precision_vs_1000g": lambda: self._gatk_precision(dataset),
            "update": self._update,
        }
        for name, check in checks.items():
            try:
                check()
            except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as e:
                # A missing or unreadable output fails its check; it never skips it.
                self.record(f"output.{name}", False, f"could not check: {e!r}")

    def _help(self) -> None:
        self.record(
            "output.help", "usage: wgsextract" in self.log("help", workload=True), ""
        )

    def _bootstrap(self) -> None:
        templates = self.output("reference", "microarray", "raw_file_templates")
        self.record("output.bootstrap", templates.is_dir(), str(templates))

    def _info(self) -> None:
        info = json.loads(
            self.output("info", "HG00096.hg19-mini.bam.wgse_info.json").read_text()
        )
        cram = json.loads(
            self.output(
                "info_cram", "HG00096.hg19-mini.cram.wgse_info.json"
            ).read_text()
        )
        chr20 = next(
            line.split(",")
            for line in info["chrom_table_csv"].splitlines()
            if line.startswith("20,")
        )
        depth = float(chr20[5])
        ok = (
            info["ref_model_str"].startswith("hg19")
            and info["avg_read_len"] == 100.0
            and info["is_paired"]
            and 3 <= depth <= 8
            and cram["md5_signature"] == info["md5_signature"]
        )
        self.record(
            "output.info",
            ok,
            f"model={info['ref_model_str']!r} read_len={info['avg_read_len']} "
            f"paired={info['is_paired']} chr20_depth={depth} "
            f"bam_md5={info['md5_signature']} cram_md5={cram['md5_signature']}",
        )

    def _mt_bam(self) -> None:
        bam = self.output("mt_bam", "HG00096.hg19-mini_mtDNA.bam")
        reads = int(self.run_tool("samtools", "view", "-c", str(bam)).strip())
        self.record("output.mt_bam", 250_000 <= reads <= 290_000, f"{reads} reads")

    def _mito_vcf(self) -> None:
        records = self.count_records(
            self.output("mito_vcf", "HG00096.hg19-mini_MT.vcf.gz")
        )
        self.record("output.mito_vcf", 8 <= records <= 25, f"{records} variants")

    def _mt_haplogroup(self) -> None:
        text = self.output("mt_haplo", "haplogrep_results.txt").read_text()
        match = re.search(r'"HG00096"\t"([^"]+)"', text)
        haplogroup = match.group(1) if match else None
        self.record(
            "output.mt_haplogroup",
            haplogroup == HG00096_MT_HAPLOGROUP,
            f"got {haplogroup}, published {HG00096_MT_HAPLOGROUP}",
        )

    def _truth_snps(self, dataset: Path, region: str, tmp: Path) -> Path:
        truth = tmp / "truth.vcf.gz"
        self.run_tool(
            "bcftools", "view", "-i", 'GT="alt"', "-v", "snps", "-r", region,
            str(dataset / "HG00096.hg19-mini.vcf.gz"), "-Oz", "-o", str(truth),
        )  # fmt: skip
        self.run_tool("bcftools", "index", "-t", "-f", str(truth))
        return truth

    def _intersect(self, truth: Path, calls: Path, tmp: Path) -> tuple[int, int, int]:
        snps = tmp / "calls.vcf.gz"
        self.run_tool(
            "bcftools", "view", "-v", "snps", str(calls), "-Oz", "-o", str(snps)
        )
        self.run_tool("bcftools", "index", "-t", "-f", str(snps))
        isec = tmp / "isec"
        self.run_tool(
            "bcftools", "isec", "-c", "none", "-p", str(isec), str(truth), str(snps)
        )
        counts = [
            sum(1 for line in (isec / f"000{i}.vcf").open() if not line.startswith("#"))
            for i in range(3)
        ]
        return counts[0], counts[1], counts[2]  # truth only, calls only, shared

    def _snp_recall(self, dataset: Path) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            truth = self._truth_snps(dataset, "20", tmp)
            missed, _extra, shared = self._intersect(
                truth, self.output("snp", "snps.vcf.gz"), tmp
            )
        recall = shared / max(shared + missed, 1)
        self.record(
            "output.snp_recall_vs_1000g",
            recall >= 0.80,
            f"{shared}/{shared + missed} published HG00096 chr20 SNPs called ({recall:.1%})",
        )

    def _gatk_precision(self, dataset: Path) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            truth = self._truth_snps(dataset, "20:1-200000", tmp)
            _missed, extra, shared = self._intersect(
                truth, self.output("gatk", "gatk.vcf.gz"), tmp
            )
        precision = shared / max(shared + extra, 1)
        self.record(
            "output.gatk_precision_vs_1000g",
            shared >= 100 and precision >= 0.85,
            f"{shared} of {shared + extra} GATK SNP calls match 1000 Genomes ({precision:.1%})",
        )

    def _microarray(self, dataset: Path) -> None:
        # The 23andMe file uses real hg19 positions, which the rebased mini genome
        # doesn't cover, so it is all no-calls; the CombinedKit uses the dataset's
        # own targets and must agree with HG00096's 1000 Genomes genotypes.
        kit_23andme = self.output("microarray", "HG00096.hg19-mini_23andMe_V5.txt")
        rows_23andme = sum(1 for line in kit_23andme.open() if not line.startswith("#"))
        truth: dict[tuple[str, str], str] = {}
        query = self.run_tool(
            "bcftools", "query", "-f", "%CHROM\t%POS\t[%TGT]\n",
            str(dataset / "HG00096.hg19-mini.vcf.gz"),
        )  # fmt: skip
        for line in query.splitlines():
            chrom, pos, tgt = line.split("\t")
            truth[(chrom, pos)] = "".join(sorted(tgt.replace("|", "").replace("/", "")))
        agree = total = 0
        kit = self.output("microarray", "HG00096.hg19-mini_CombinedKit.txt")
        for line in kit.open():
            if line.startswith("#"):
                continue
            _rsid, chrom, pos, genotype = line.rstrip("\n").split("\t")[:4]
            if (chrom, pos) in truth:
                total += 1
                agree += "".join(sorted(genotype)) == truth[(chrom, pos)]
        concordance = agree / max(total, 1)
        self.record(
            "output.microarray",
            rows_23andme > 600_000 and total >= 1500 and concordance >= 0.95,
            f"23andMe rows {rows_23andme}; CombinedKit {agree}/{total} genotypes "
            f"match 1000 Genomes ({concordance:.1%})",
        )

    def _align(self) -> None:
        bam = self.output("align", "HG00096.hg19-mini_aligned.bam")
        flagstat = self.run_tool("samtools", "flagstat", str(bam))
        match = re.search(r"mapped \(([\d.]+)%", flagstat)
        mapped = float(match.group(1)) if match else 0.0
        self.record("output.align", mapped >= 99.0, f"{mapped}% mapped")

    def _update(self) -> None:
        kept = self.log("update_kept_data").strip()
        self.record("output.update_kept_reference", kept == self.nonce, f"{kept!r}")
        text = (self.results / "post_update_mt" / "haplogrep_results.txt").read_text()
        self.record(
            "output.post_update_mt_haplogroup", HG00096_MT_HAPLOGROUP in text, ""
        )

    def check_state(self, noise_patterns: list[str]) -> None:
        def load(name: str) -> set[str]:
            path = self.results / f"inventory-{name}.txt"
            if not path.is_file():
                return set()
            return set(path.read_text().splitlines())

        baseline, installed, final = (
            load("baseline"),
            load("after_install"),
            load("after_uninstall"),
        )
        errors = sorted(
            line for line in baseline | installed | final if line.startswith("error\t")
        )
        if not self.record(
            "state.snapshots",
            bool(baseline and installed and final) and not errors,
            "\n  ".join(errors),
        ):
            return

        def significant(lines: set[str]) -> list[str]:
            keep = []
            for line in sorted(lines):
                path = line.split("\t")[1] if "\t" in line else line
                if not any(
                    fnmatch.fnmatch(path, p) or fnmatch.fnmatch(line, p)
                    for p in noise_patterns
                ):
                    keep.append(line)
            return keep

        # A tool that crashed leaves a crash report even if its step "succeeded"
        # (e.g. a version probe); none may appear, whoever owns the process.
        crashes = sorted(
            line.split("\t")[1]
            for line in final | installed
            if (
                ("/DiagnosticReports/" in line and line.endswith((".ips", ".crash")))
                # Windows Error Reporting and user-mode crash dumps
                or re.search(r"/WER/Report(Archive|Queue)/[^/]+$", line)
                or re.search(r"/CrashDumps/[^/]+\.dmp$", line, re.IGNORECASE)
            )
            and line not in baseline
        )
        self.record(
            "state.no_crash_reports",
            not crashes,
            "\n  ".join(["crashed:", *crashes]) if crashes else "none",
        )

        # The snapshots must see the install, or the uninstall check proves nothing.
        installed_added = significant(installed - baseline)
        self.record(
            "state.install_visible",
            any(self.install_marker in line for line in installed_added),
            f"{len(installed_added)} paths added by install",
        )
        added = significant(final - baseline)
        removed = significant(baseline - final)
        self.record(
            "state.uninstall_restores_baseline",
            not added and not removed,
            "left behind:\n  " + "\n  ".join(added[:40])
            if added
            else ("removed:\n  " + "\n  ".join(removed[:40]) if removed else "clean"),
        )

    def run(self, dataset: Path, noise_patterns: list[str]) -> list[Check]:
        self.check_run_identity()
        self.check_clean_baseline()
        self.check_steps()
        self.check_outputs(dataset)
        self.check_state(noise_patterns)
        return self.checks


def find_tools() -> dict[str, str]:
    """Host measuring tools: the repo's Pixi env, else PATH."""
    repo_env = (
        Path(__file__).resolve().parents[2] / ".pixi" / "envs" / "default" / "bin"
    )
    tools = {}
    for tool in ("samtools", "bcftools"):
        candidate = repo_env / tool
        found = str(candidate) if candidate.exists() else shutil.which(tool)
        if not found:
            raise SystemExit(f"oracle needs {tool} on the host (run `pixi install`).")
        tools[tool] = found
    return tools
