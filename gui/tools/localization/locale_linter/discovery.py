"""Bundle discovery for localization linting."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from .models import BundleTarget

try:
    from tools.json_comments import loads_json_with_comments
except ModuleNotFoundError:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from tools.json_comments import loads_json_with_comments

_BUILTIN_DIR = Path("resources/BuiltinStrings")


def discover_bundles(paths: list[str]) -> list[BundleTarget]:
    bundles: list[BundleTarget] = []
    cwd = Path.cwd()

    if not paths:
        builtin_dir = cwd / _BUILTIN_DIR
        if builtin_dir.is_dir():
            target = _build_target(builtin_dir, requires_builtin=True, bundle_root=builtin_dir.parent)
            if target is not None:
                bundles.append(target)
        examples = cwd / "examples"
        if examples.is_dir():
            for entry in sorted(examples.iterdir(), key=lambda p: str(p)):
                if entry.is_dir():
                    target = _build_target(entry / "strings", requires_builtin=False, bundle_root=entry)
                    if target is not None:
                        bundles.append(target)
        return bundles

    for raw in paths:
        path = Path(raw)
        if not path.exists():
            sys.stderr.write(f"Path does not exist: {raw}\n")
            continue
        if path.is_dir():
            strings_dir, bundle_root, requires_builtin = _target_parts_for_directory(path)
        else:
            strings_dir = path.parent
            bundle_root = strings_dir.parent
            requires_builtin = "BuiltinStrings" in strings_dir.parts
        target = _build_target(strings_dir, requires_builtin=requires_builtin, bundle_root=bundle_root)
        if target is None:
            sys.stderr.write(f"Could not lint strings folder at {path}\n")
        else:
            bundles.append(target)

    return bundles


def _target_parts_for_directory(path: Path) -> tuple[Path, Path, bool]:
    if path.name == "strings" and (path.parent / "manifest.json").exists():
        return path, path.parent, False
    if (path / "manifest.json").exists():
        return path / "strings", path, False
    requires_builtin = _is_builtin_path(path)
    return path, path.parent, requires_builtin


def _build_target(strings_dir: Path, *, requires_builtin: bool, bundle_root: Path) -> Optional[BundleTarget]:
    if not strings_dir.is_dir():
        return None
    source_code = "en" if requires_builtin else _read_default_locale_code(bundle_root)
    source_path = strings_dir / f"strings.{source_code}.toml"
    if not source_path.exists():
        sys.stderr.write(
            f"Source file {source_path} not found (expected for default locale {source_code!r}).\n"
        )
        return None
    locales: list[tuple[str, Path]] = []
    for entry in sorted(strings_dir.iterdir(), key=lambda p: p.name):
        name = entry.name
        if not (name.startswith("strings.") and name.endswith(".toml")):
            continue
        inner = name[len("strings.") : -len(".toml")]
        if inner != source_code:
            locales.append((inner, entry))
    return BundleTarget(
        name="BuiltinStrings" if requires_builtin else strings_dir.parent.name,
        directory=strings_dir,
        source_path=source_path,
        source_code=source_code,
        locales=locales,
        requires_builtin=requires_builtin,
    )


def _read_default_locale_code(bundle_root: Path) -> str:
    manifest_path = bundle_root / "manifest.json"
    if manifest_path.exists():
        try:
            data = loads_json_with_comments(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "en"
        code = data.get("defaultLocalizationCode")
        if isinstance(code, str) and code.strip():
            return code.strip()
    return "en"


def _is_builtin_path(path: Path) -> bool:
    try:
        path.resolve().relative_to(_BUILTIN_DIR.resolve())
        return True
    except ValueError:
        return "BuiltinStrings" in path.parts
