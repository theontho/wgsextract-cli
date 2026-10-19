"""Judge an install-lab run of the WGS Extract macOS app (guest/gui-macos.sh).

Same rules as the CLI oracle: fail closed, evidence tied to the run's nonce,
expected values from biology, and the machine must end as it started. What the
app reports about itself (setup and uninstall result files, the bundle test
report) is evidence to check, never a verdict to trust.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from oracle import Oracle

GUI_STEPS = [
    "download_dmg",
    "install_app",
    "gatekeeper",
    "setup",
    "runner",
    "actions",
    "uninstall",
]
PLAN = Path(__file__).resolve().parent / "gui" / "wgsextract-plan.json"
APP_PATH = "/Applications/WGSExtract.app"
BUNDLE_ID = "dev.guiforcli.embed.wgsextract"


class GuiOracle(Oracle):
    install_marker = f"/{BUNDLE_ID}/BundleWorkspaces"

    def __init__(
        self, results: Path, nonce: str, tools: dict[str, str], signed: bool
    ) -> None:
        super().__init__(results, nonce, tools)
        self.signed = signed

    def output(self, *parts: str) -> Path:
        return self.results.joinpath("workload", *parts)

    def result_file(self, name: str) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.results / name).read_text())
        return data

    def check_steps(self) -> None:
        for step in GUI_STEPS:
            rc = self.rc(step)
            if step == "gatekeeper" and not self.signed:
                # An ad-hoc build must be rejected, or the check proves nothing.
                rejected = "rejected" in self.log(step)
                self.record(
                    "step.gatekeeper",
                    rc not in (None, 0) and rejected,
                    f"unsigned build: exit code {rc}, "
                    f"{'rejected' if rejected else 'NOT rejected'} by Gatekeeper",
                )
            else:
                self.record(f"step.{step}", rc == 0, f"exit code {rc}")

    def check_app_reports(self) -> None:
        try:
            startup = (self.results / "startup.txt").read_text()
        except OSError as e:
            startup = repr(e)
        self.record(
            "app.window_appeared",
            "window_appeared_ms=" in startup,
            startup.strip().splitlines()[0] if startup.strip() else "empty",
        )
        try:
            setup = self.result_file("setup-result.json")
            failed = [r["id"] for r in setup.get("results", []) if r["status"] != "ok"]
            self.record(
                "app.setup",
                setup["status"] == "ok" and bool(setup.get("results")) and not failed,
                f"status {setup['status']}; {len(setup.get('results', []))} steps"
                + (f"; not ok: {', '.join(failed)}" if failed else ""),
            )
        except (OSError, ValueError, KeyError) as e:
            self.record("app.setup", False, f"no setup result: {e!r}")
        try:
            report = self.result_file("bundle-report.json")
            planned = [s["id"] for s in json.loads(PLAN.read_text())["steps"]]
            statuses = {s.get("id"): s["status"] for s in report["steps"]}
            not_passed = [
                f"{i}={statuses.get(i)}" for i in planned if statuses.get(i) != "passed"
            ]
            self.record(
                "app.actions",
                not not_passed,
                f"{len(planned) - len(not_passed)}/{len(planned)} planned actions passed"
                + (f"; {', '.join(not_passed)}" if not_passed else ""),
            )
        except (OSError, ValueError, KeyError) as e:
            self.record("app.actions", False, f"no bundle report: {e!r}")
        try:
            uninstall = self.result_file("uninstall-result.json")
            self.record(
                "app.uninstall",
                uninstall["status"] == "ok",
                "; ".join(uninstall.get("log", []))[:400],
            )
        except (OSError, ValueError, KeyError) as e:
            self.record("app.uninstall", False, f"no uninstall result: {e!r}")

    def check_outputs(self, dataset: Path) -> None:
        checks = {
            "info": self._info_bam,
            "mt_bam": self._mt_bam,
            "mito_vcf": self._mito_vcf,
            "mt_haplogroup": self._mt_haplogroup,
            "snp_recall_vs_1000g": lambda: self._snp_recall(dataset),
            "microarray": self._microarray_format,
            "align": self._align,
            "gatk_precision_vs_1000g": lambda: self._gatk_precision(dataset),
        }
        for name, check in checks.items():
            try:
                check()
            except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as e:
                self.record(f"output.{name}", False, f"could not check: {e!r}")

    def _info_bam(self) -> None:
        info = json.loads(
            self.output("info", "HG00096.hg19-mini.bam.wgse_info.json").read_text()
        )
        ok = (
            info["ref_model_str"].startswith("hg19")
            and info["avg_read_len"] == 100.0
            and info["is_paired"]
        )
        self.record(
            "output.info",
            ok,
            f"model={info['ref_model_str']!r} read_len={info['avg_read_len']} "
            f"paired={info['is_paired']}",
        )

    def _microarray_format(self) -> None:
        # The app has no --ref-vcf-tab, so on the rebased mini genome only the
        # kit's format is checkable; genotype concordance is the CLI lab's job.
        kit = self.output("microarray", "HG00096.hg19-mini_23andMe_V5.txt")
        rows = sum(1 for line in kit.open() if not line.startswith("#"))
        self.record(
            "output.microarray",
            rows > 600_000,
            f"23andMe rows {rows} (format only; concordance not measurable here)",
        )

    def check_state(self, noise_patterns: list[str]) -> None:
        super().check_state(noise_patterns)
        installed = (self.results / "inventory-after_install.txt").read_text()
        self.record(
            "state.app_installed",
            f"\t{APP_PATH}" in installed,
            APP_PATH,
        )

    def run(self, dataset: Path, noise_patterns: list[str]) -> list:
        self.check_run_identity()
        self.check_gatekeeper_enabled()
        self.check_clean_baseline()
        self.check_steps()
        self.check_app_reports()
        self.check_outputs(dataset)
        self.check_state(noise_patterns)
        return self.checks
