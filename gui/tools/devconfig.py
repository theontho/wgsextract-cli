from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parents[1]
DEVCONFIG_PATH = REPO_ROOT / ".devconfig.toml"


@lru_cache(maxsize=1)
def load_devconfig() -> dict[str, Any]:
    if not DEVCONFIG_PATH.exists():
        return {}
    with DEVCONFIG_PATH.open("rb") as handle:
        if tomllib is not None:
            return tomllib.load(handle)
        return parse_simple_toml(handle.read().decode("utf-8"))



def reload_devconfig() -> dict[str, Any]:
    load_devconfig.cache_clear()
    return load_devconfig()



def get_path(*keys: str, default: Any = None) -> Any:
    current: Any = load_devconfig()
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current



def env_or_config(env_name: str, *config_path: str, default: str = "") -> str:
    value = os.environ.get(env_name)
    if value is not None:
        return value
    config_value = get_path(*config_path, default=default)
    return config_value if isinstance(config_value, str) else default


def parse_simple_toml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    section: dict[str, Any] = root
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = root
            for part in line[1:-1].split("."):
                part = part.strip()
                if not part:
                    continue
                value = section.setdefault(part, {})
                if not isinstance(value, dict):
                    raise ValueError(f"Cannot create TOML section under scalar key: {part}")
                section = value
            continue
        key, separator, raw_value = line.partition("=")
        if not separator:
            continue
        value = raw_value.strip()
        if value.startswith('"'):
            section[key.strip()] = parse_quoted_string(value)
        else:
            comment_index = value.find(" #")
            if comment_index >= 0:
                value = value[:comment_index].strip()
            section[key.strip()] = value
    return root


def parse_quoted_string(value: str) -> str:
    escaped = False
    for index, char in enumerate(value[1:], start=1):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return value[1:index].replace(r"\"", '"').replace(r"\\", "\\")
    return value
