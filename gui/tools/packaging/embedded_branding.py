from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_parent = Path(__file__).resolve().parents[2]
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))
from tools.devconfig import get_path
from tools.json_comments import loads_json_with_comments


DEFAULT_EMBEDDED_BUNDLE_PATH = "examples/WGSExtract"
DISTRIBUTION_SUFFIXES = (
    "Linux AppImage WebUI",
    "macOS WebUI",
    "Windows WebUI",
    "Ubuntu WebUI",
    "Fedora WebUI",
    "Linux WebUI",
    "Arch WebUI",
    "macOS",
    "WebUI",
)


def app_name_without_distribution_suffix(app_name: str | None) -> str | None:
    if app_name is None:
        return None
    stripped = app_name.strip()
    if not stripped:
        return None
    normalized = stripped.lower()
    for suffix in sorted(DISTRIBUTION_SUFFIXES, key=len, reverse=True):
        normalized_suffix = suffix.lower()
        if normalized.endswith(f" {normalized_suffix}"):
            base_name = stripped[: -(len(suffix) + 1)].strip()
            return base_name or stripped
    return stripped


@dataclass(frozen=True)
class EmbeddedBranding:
    bundle_path: Path | None
    app_name: str | None
    app_version: str | None

    @property
    def enabled(self) -> bool:
        return self.bundle_path is not None or self.app_name is not None

    @property
    def effective_app_name(self) -> str | None:
        if self.app_name:
            return app_name_without_distribution_suffix(self.app_name)
        if self.bundle_path is not None:
            return self.bundle_path.name
        return None

    @property
    def effective_app_version(self) -> str | None:
        return self.app_version


def env_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def load_embedded_branding(repo_root: Path) -> EmbeddedBranding:
    bundle_value = (
        env_value("EMBEDDED_BUNDLE_PATH", "PACKAGE_BUNDLE_PATH")
        or get_path("packaging", "embedded_bundle_path", default="")
        or DEFAULT_EMBEDDED_BUNDLE_PATH
    )
    bundle_path: Path | None = None
    if bundle_value:
        candidate = Path(bundle_value)
        bundle_path = candidate if candidate.is_absolute() else repo_root / candidate
        bundle_path = bundle_path.resolve()
        if not bundle_path.exists():
            raise FileNotFoundError(
                f"Embedded bundle path does not exist: {bundle_path}"
            )
        manifest_path = bundle_path / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(
                f"Embedded bundle is missing manifest.json: {manifest_path}"
            )
        manifest = loads_json_with_comments(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {}

    app_name = (
        env_value("PACKAGE_APP_NAME", "EMBEDDED_APP_NAME")
        or get_path("packaging", "app_name", default="")
        or None
    )
    app_version = (
        env_value("PACKAGE_APP_VERSION", "EMBEDDED_APP_VERSION")
        or get_path("packaging", "app_version", default="")
        or string_value(manifest.get("version"))
        or None
    )
    return EmbeddedBranding(
        bundle_path=bundle_path, app_name=app_name, app_version=app_version
    )


def string_value(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""
