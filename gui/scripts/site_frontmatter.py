from __future__ import annotations

import json
from pathlib import Path

try:
    from .site_model import ROOT, Page
except ImportError:  # pragma: no cover - script execution path
    from site_model import ROOT, Page


def load_page(source: Path) -> Page:
    metadata, body = parse_front_matter(source.read_text(encoding="utf-8"))
    return Page(
        source=source,
        output_name=require(metadata, "output"),
        title=require(metadata, "title"),
        description=require(metadata, "description"),
        eyebrow=require(metadata, "eyebrow"),
        heading=require(metadata, "heading"),
        lede=require(metadata, "lede"),
        footer_title=metadata.get("footer_title", "GUI for CLI"),
        footer_text=metadata.get(
            "footer_text",
            "Turn CLI bundles into desktop apps without rewriting the tool.",
        ),
        actions=parse_actions(metadata.get("actions", "")),
        auto_hero=parse_bool(metadata.get("auto_hero", "true"), source, "auto_hero"),
        body=body,
    )


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("Markdown source must start with front matter")

    metadata: dict[str, str] = {}
    index = 1
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError(f"Invalid front matter line: {line}")
        metadata[key.strip()] = unquote_scalar(value.strip())
    else:
        raise ValueError("Unterminated front matter")

    return metadata, "\n".join(lines[index:]).strip()


def unquote_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid double-quoted scalar: {value}") from error
    return value


def require(metadata: dict[str, str], key: str) -> str:
    value = metadata.get(key, "")
    if not value:
        raise ValueError(f"Missing required front matter key: {key}")
    return value


def parse_bool(value: str, source: Path, key: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    relative = source.relative_to(ROOT)
    raise ValueError(f"{relative}: {key} must be a boolean value")


def parse_actions(value: str) -> tuple[tuple[str, str, str], ...]:
    if not value:
        return ()
    actions: list[tuple[str, str, str]] = []
    for item in value.split(";"):
        parts = [part.strip() for part in item.split("|")]
        if len(parts) not in {2, 3}:
            raise ValueError(f"Expected label|href or label|href|class action: {item}")
        label, href = parts[:2]
        if not label or not href:
            raise ValueError(f"Expected non-empty label and href in action: {item}")
        css_class = parts[2] if len(parts) == 3 else ""
        actions.append((label, href, css_class))
    return tuple(actions)
