#!/usr/bin/env python3
"""Generate docs/api.md from the docstrings and Params fields of wgsextract_cli.api.

python scripts/gen_api_docs.py          # rewrite docs/api.md
python scripts/gen_api_docs.py --check  # exit 1 if docs/api.md is stale
"""

from __future__ import annotations

import argparse
import inspect
import os
import re
import sys
import typing
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wgsextract_cli import api  # noqa: E402
from wgsextract_cli.api._params import (  # noqa: E402
    ParamField,
    Params,
    param_fields,
    type_name,
)

OUTPUT = ROOT / "docs" / "api.md"
GROUPS = (
    api.info,
    api.extract,
    api.bam,
    api.qc,
    api.align,
    api.pet,
    api.vcf,
    api.analyze,
    api.lineage,
    api.methylation,
    api.microarray,
    api.ref,
    api.repair,
    api.sign,
    api.genotyping,
    api.report,
    api.sample,
    api.support,
    api.deps,
    api.examples,
    api.vep,
    api.benchmark,
)
RUNTIME = (api.ProgressEvent, api.ProgressCallback, api.CancelToken, api.CommandFailed)


def docstring(obj: object) -> str:
    """A docstring as Markdown: reST ``::`` blocks become fenced code."""
    text = re.sub(r"``([^`]+)``", r"`\1`", inspect.getdoc(obj) or "")
    out: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.endswith("::"):
            out.append(line[:-1] if not line.endswith(" ::") else line[:-3])
            i += 1
            block: list[str] = []
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith(" ")):
                block.append(lines[i])
                i += 1
            code = inspect.cleandoc("\n".join(block))
            out += ["", "```python", code, "```", ""]
            continue
        out.append(line)
        i += 1
    return "\n".join(out).strip()


def cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def default_text(item: ParamField) -> str:
    if item.required:
        return "required"
    default = item.default
    if isinstance(default, tuple):
        default = list(default)
    elif isinstance(default, os.PathLike):
        default = os.fspath(default)
    text = f"`{default!r}`"
    if item.spec.config_key:
        text += f" (config `{item.spec.config_key}`)"
    return text


def field_table(cls: type[Params]) -> Iterator[str]:
    yield "| Field | Type | Default | CLI | Description |"
    yield "| --- | --- | --- | --- | --- |"
    for item in param_fields(cls):
        flags = ", ".join(f"`{flag}`" for flag in item.flags)
        description = item.spec.description
        if item.spec.choices:
            if description and not description.endswith((".", ")")):
                description += "."
            description += " One of: " + ", ".join(
                f"`{choice}`" for choice in item.spec.choices
            )
        yield (
            f"| `{item.name}` | `{cell(type_name(item.type))}` | "
            f"{cell(default_text(item))} | {flags} | {cell(description)} |"
        )


def defined_in(module: ModuleType, predicate: typing.Callable[[object], bool]) -> list:
    members = [
        obj
        for name, obj in vars(module).items()
        if not name.startswith("_")
        and predicate(obj)
        and getattr(obj, "__module__", None) == module.__name__
    ]
    return sorted(members, key=lambda obj: inspect.getsourcelines(obj)[1])


def group_section(module: ModuleType) -> Iterator[str]:
    short = module.__name__.rsplit(".", 1)[1]
    yield f"## `api.{short}`"
    yield ""
    yield docstring(module)
    yield ""
    for function in defined_in(module, inspect.isfunction):
        cls = typing.get_type_hints(function).get("params")
        if cls is None:
            signature = str(inspect.signature(function)).replace("'", "")
            yield f"### `api.{short}.{function.__name__}{signature}`"
            yield ""
            yield docstring(function)
            yield ""
            continue
        yield f"### `api.{short}.{function.__name__}({cls.__name__})`"
        yield ""
        yield f"CLI: `wgsextract {cls.COMMAND}`."
        yield ""
        yield docstring(function)
        yield ""
        yield f"`{cls.__name__}`: {docstring(cls)}"
        yield ""
        yield from field_table(cls)
        yield ""
    for name in getattr(module, "__all__", ()):
        obj = getattr(module, name)
        if inspect.isclass(obj) and getattr(obj, "__module__", None) != module.__name__:
            yield f"### `api.{short}.{name}`"
            yield ""
            yield docstring(obj)
            yield ""
            yield from members(obj)


def runtime_section() -> Iterator[str]:
    yield "## Results, progress and cancellation"
    yield ""
    yield (
        "`api.Result` is the `CommandResult` model of the "
        "[result manifest](result-manifest.md): `status`, `exit_code`, "
        "`error`, `inputs`, `outputs`, `metrics`, `warnings`, `details`, "
        "`manifest` and `provenance`; `to_dict()` and `to_json()` give the "
        "`wgsextract.result/1` document. `text` is the human-readable report "
        "the CLI prints (without `--json`); it is not in the manifest. "
        "`api.json_schema(cls)` gives a JSON "
        "Schema of a Params class; `wgsextract help --all --json` includes it "
        "as each typed command's `input_schema`, with each property's flag "
        "as `x-cli-flag` (a positional argument's metavar as "
        "`x-cli-positional`)."
    )
    yield ""
    for obj in RUNTIME:
        yield f"### `api.{obj.__name__}`"
        yield ""
        yield docstring(obj)
        yield ""
        yield from members(obj)


def members(obj: type) -> Iterator[str]:
    """A bullet per documented public method or property of ``obj``."""
    methods = [
        (name, member)
        for name, member in vars(obj).items()
        if not name.startswith("_")
        and (inspect.isfunction(member) or isinstance(member, property))
        and inspect.getdoc(member)
    ]
    for name, member in methods:
        yield f"- `{name}`: {' '.join(docstring(member).split())}"
    if methods:
        yield ""


def render() -> str:
    lines = [
        "# Python API",
        "",
        "<!-- Generated by scripts/gen_api_docs.py from the docstrings and "
        "Params fields of wgsextract_cli.api; do not edit. -->",
        "",
        docstring(api),
        "",
        *runtime_section(),
    ]
    for module in GROUPS:
        lines += group_section(module)
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="Fail if docs/api.md is out of date."
    )
    text = render()
    if parser.parse_args().check:
        if OUTPUT.read_text(encoding="utf-8") != text:
            print(f"{OUTPUT.relative_to(ROOT)} is stale; run {__file__}.")
            return 1
        return 0
    OUTPUT.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
