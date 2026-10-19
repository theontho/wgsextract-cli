"""Judge an install-lab run of the WGS Extract app: the macOS app
(guest/gui-macos.sh) or the Windows app (guest/windows/gui-scenario.ps1).

Same rules as the CLI oracle: fail closed, evidence tied to the run's nonce,
expected values from biology, and the machine must end as it started. What the
app reports about itself (setup and uninstall results, the bundle test report)
is evidence to check, never a verdict to trust.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oracle import Oracle

PLAN = Path(__file__).resolve().parent / "gui" / "wgsextract-plan.json"


@dataclass(frozen=True)
class AppPlatform:
    steps: list[str]
    # The step that checks the download's signature: an unsigned (local) build
    # must be refused, with this text in its log, or the check proves nothing.
    signature_step: str
    signed_text: str
    unsigned_text: str
    # A file the app writes once its window has drawn the bundle's UI.
    window_file: str
    window_text: str
    # Inventory lines (exact) that prove the app was installed.
    installed: list[str]
    # A path the install must visibly add (see Oracle.install_marker).
    install_marker: str


MACOS = AppPlatform(
    steps=[
        "download_dmg",
        "install_app",
        "gatekeeper",
        "setup",
        "runner",
        "actions",
        "uninstall",
    ],
    signature_step="gatekeeper",
    signed_text="accepted",
    unsigned_text="rejected",
    window_file="startup.txt",
    window_text="window_appeared_ms=",
    installed=["dir\t/Applications/WGSExtract.app"],
    install_marker="/dev.guiforcli.embed.wgsextract/BundleWorkspaces",
)
_WIN_APP = "C:/Users/lab user/AppData/Local/WGSExtract"
WINDOWS = AppPlatform(
    steps=[
        "download_installer",
        "signature",
        "install_app",
        "setup",
        "actions",
        "uninstall",
    ],
    signature_step="signature",
    signed_text="Status: Valid",
    unsigned_text="Status: NotSigned",
    # The window loaded the page (main.rs); that the page ran is shown by the setup it
    # started. (Its webAppRendered metric isn't printed on Windows.)
    window_file="app-stdout.txt",
    window_text="metric webNavigationDidFinish_ms=",
    installed=[
        f"file\t{_WIN_APP}/gui-for-cli-webui-tauri.exe",
        "regkey\tHKEY_CURRENT_USER/Software/Microsoft/Windows/CurrentVersion/Uninstall/WGSExtract",
        "file\tC:/Users/lab user/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/WGSExtract.lnk",
    ],
    install_marker="/dev.guiforcli.web.embed.wgsextract/BundleWorkspaces",
)
LINUX = AppPlatform(
    steps=["download_installer", "install_app", "setup", "actions", "uninstall"],
    # apt installs a downloaded .deb without checking a signature.
    signature_step="",
    signed_text="",
    unsigned_text="",
    window_file="app-stdout.txt",
    window_text="metric webNavigationDidFinish_ms=",
    installed=["file\t/usr/share/applications/WGSExtract.desktop"],
    install_marker="/dev.guiforcli.web.embed.wgsextract/BundleWorkspaces",
)


class GuiOracle(Oracle):
    def __init__(
        self, results: Path, nonce: str, tools: dict[str, str], signed: bool
    ) -> None:
        super().__init__(results, nonce, tools)
        self.signed = signed
        self.app = (
            WINDOWS
            if self.target.startswith("windows")
            else LINUX
            if self.target == "linux"
            else MACOS
        )
        self.install_marker = self.app.install_marker

    def output(self, *parts: str) -> Path:
        return self.results.joinpath("workload", *parts)

    def result_file(self, name: str) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.results / name).read_text())
        return data

    def check_steps(self) -> None:
        for step in self.app.steps:
            rc = self.rc(step)
            if step != self.app.signature_step:
                self.record(f"step.{step}", rc == 0, f"exit code {rc}")
            elif self.signed:
                found = self.app.signed_text in self.log(step)
                self.record(
                    f"step.{step}",
                    rc == 0 and found,
                    f"signed build: exit code {rc}, {self.app.signed_text!r} "
                    f"{'found' if found else 'missing'}",
                )
            else:
                # An unsigned build must be refused, or the check proves nothing.
                found = self.app.unsigned_text in self.log(step)
                self.record(
                    f"step.{step}",
                    rc not in (None, 0) and found,
                    f"unsigned build: exit code {rc}, {self.app.unsigned_text!r} "
                    f"{'found' if found else 'missing'}",
                )

    def check_app_reports(self) -> None:
        try:
            window = (self.results / self.app.window_file).read_text(errors="replace")
        except OSError as e:
            window = repr(e)
        shown = [line for line in window.splitlines() if self.app.window_text in line]
        self.record(
            "app.window_appeared",
            bool(shown),
            shown[0]
            if shown
            else f"no {self.app.window_text!r} in {self.app.window_file}",
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
            failed = [
                r.get("id") for r in uninstall.get("results", []) if r["status"] != "ok"
            ]
            self.record(
                "app.uninstall",
                uninstall["status"] == "ok" and not failed,
                "; ".join(uninstall.get("log", []))[:400]
                or f"status {uninstall['status']}; {len(uninstall.get('results', []))} steps"
                + (f"; not ok: {', '.join(map(str, failed))}" if failed else ""),
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
        try:
            installed = set(
                (self.results / "inventory-after_install.txt").read_text().splitlines()
            )
        except OSError:
            installed = set()
        missing = [line for line in self.app.installed if line not in installed]
        self.record(
            "state.app_installed",
            not missing,
            "missing: " + "; ".join(missing)
            if missing
            else "; ".join(self.app.installed),
        )

    def run(self, dataset: Path, noise_patterns: list[str]) -> list:
        self.check_run_identity()
        if self.app is MACOS:
            self.check_gatekeeper_enabled()
        self.check_clean_baseline()
        self.check_steps()
        self.check_app_reports()
        self.check_outputs(dataset)
        self.check_state(noise_patterns)
        return self.checks
