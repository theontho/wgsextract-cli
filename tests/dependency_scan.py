"""Find which external tools a command handler checks for, by reading its code.

Starting from a command's handler, follow the calls it makes (functions it
names, closures the typed-API wrapper holds, functions passed as callbacks)
through the wgsextract_cli package, and collect the string arguments of the
dependency checks. Used by tests/test_command_needs.py so the published
per-command tool lists can't drift from what the commands actually check.
"""

import ast
import functools
import inspect
import textwrap
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from wgsextract_cli.core import tool_catalog

CHECKS = {"verify_dependencies", "env_tool", "pixi_env_tool", "require_native_tool"}
PROBES = {"get_tool_path", "which"}
# Calls that start a program: a tool named in their argv is one the command runs.
RUNNERS = {"run_command", "run_pipeline", "popen", "Popen", "run", "check_output"}
# Tools the code looks up itself rather than through the tool catalog.
EXTRA_TOOLS = ("docker", "ssh-keygen")


def tool_vocabulary() -> dict[str, str]:
    """Tool names a check can mention, mapped to the name users see.

    Pixi environment launchers such as ``pharmcat_python`` map to their
    environment (``pharmcat``), which is what ``deps check`` reports.
    """
    names = [
        *tool_catalog.MANDATORY_TOOLS,
        *tool_catalog.OPTIONAL_TOOLS,
        *tool_catalog.PIXI_TOOL_ENVS,
        *EXTRA_TOOLS,
    ]
    vocab = {name: name for name in names}
    for name, env in tool_catalog.PIXI_TOOL_ENVS.items():
        if name.endswith("_python"):
            vocab[name] = env
    return vocab


@dataclass
class Scan:
    """Tools a handler can reach, by how it reaches them.

    ``checked``: required by a dependency check on every run. ``optional``:
    checked only as an option's default (``args.aligner or "bwa"``), probed
    for, or run without a check. ``mentioned``: any tool name in the reachable
    code, such as the alternatives an option compares against.
    """

    checked: set[str] = field(default_factory=set)
    optional: set[str] = field(default_factory=set)
    mentioned: set[str] = field(default_factory=set)
    warning_keys: set[str] = field(default_factory=set)


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _resolve(node: ast.expr, scope: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name):
        return scope.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, scope)
        return getattr(base, node.attr, None) if base is not None else None
    return None


@functools.cache
def _definition(func: Callable[..., Any]) -> ast.AST | None:
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    except (OSError, SyntaxError, TypeError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return node
    return None


def _defaults(expr: ast.AST) -> set[str]:
    """Literals that are only a fallback: the ``"bwa"`` of ``args.aligner or "bwa"``."""
    return {
        value.value
        for node in ast.walk(expr)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
        for value in node.values[1:]
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
        and not isinstance(node.values[0], ast.Constant)
    }  # fmt: skip


def _strings(
    expr: ast.AST,
    func: types.FunctionType,
    body: ast.AST,
    depth: int = 0,
    defaults: set[str] | None = None,
) -> set[str]:
    """String values an expression can take: literals, globals, local assignments.

    Fallback literals (see ``_defaults``) are added to ``defaults`` too.
    """
    found: set[str] = set()
    scope = func.__globals__
    if defaults is not None:
        defaults |= _defaults(expr)
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
        elif isinstance(node, ast.Name):
            value = scope.get(node.id)
            if isinstance(value, str):
                found.add(value)
            elif isinstance(value, (list, tuple)):
                found.update(v for v in value if isinstance(v, str))
            elif depth < 2:
                for assign in ast.walk(body):
                    if isinstance(assign, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == node.id
                        for t in assign.targets
                    ):
                        found |= _strings(assign.value, func, body, depth + 1, defaults)
    return found


def _params(definition: ast.AST) -> list[str]:
    if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return [a.arg for a in definition.args.args]
    return []


@functools.cache
def _forwarded_param(func: types.FunctionType) -> int | None:
    """Index of the parameter ``func`` hands to verify_dependencies, if any."""
    definition = _definition(func)
    if definition is None:
        return None
    params = _params(definition)
    for node in ast.walk(definition):
        if not (
            isinstance(node, ast.Call)
            and _call_name(node) == "verify_dependencies"
            and node.args
        ):
            continue
        # verify_dependencies(tools) or verify_dependencies([tool])
        arg = node.args[0]
        names = arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg]
        for name in names:
            if isinstance(name, ast.Name) and name.id in params:
                return params.index(name.id)
    return None


def _argument(node: ast.Call, target: types.FunctionType, index: int):
    if index < len(node.args):
        return node.args[index]
    definition = _definition(target)
    name = _params(definition)[index] if definition is not None else None
    return next((k.value for k in node.keywords if k.arg == name), None)


def scan(handler: Callable[..., Any]) -> Scan:
    """Tools ``handler`` can use, in user-facing names (see ``Scan``)."""
    result = Scan()
    raw = Scan()

    def check(expr: ast.AST, func: types.FunctionType, body: ast.AST) -> None:
        defaults: set[str] = set()
        found = _strings(expr, func, body, 0, defaults)
        raw.checked |= found - defaults
        raw.optional |= defaults

    seen: set[types.FunctionType] = set()
    queue: list[Any] = [handler]
    while queue:
        func = inspect.unwrap(queue.pop())
        if isinstance(func, functools.partial):
            func = func.func
        if (
            not isinstance(func, types.FunctionType)
            or func in seen
            or not func.__module__.startswith("wgsextract_cli")
        ):
            continue
        seen.add(func)
        for cell in func.__closure__ or ():
            contents = cell.cell_contents
            if isinstance(contents, types.FunctionType):
                queue.append(contents)
        body = _definition(func)
        if body is None:
            continue
        scope = func.__globals__
        for node in ast.walk(body):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                raw.mentioned.add(node.value)
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if node.args:
                if name in CHECKS:
                    check(node.args[0], func, body)
                elif name in PROBES | RUNNERS:
                    raw.optional |= _strings(node.args[0], func, body)
                elif name == "print_warning":
                    raw.warning_keys |= _strings(node.args[0], func, body)
            target = _resolve(node.func, scope)
            if isinstance(target, types.FunctionType):
                queue.append(target)
                index = _forwarded_param(target)
                if index is not None:
                    expr = _argument(node, target, index)
                    if expr is not None:
                        check(expr, func, body)
            for arg in node.args:
                callback = _resolve(arg, scope)
                if isinstance(callback, types.FunctionType):
                    queue.append(callback)
    vocab = tool_vocabulary()

    def names(raw_names: set[str]) -> set[str]:
        return {vocab[t] for t in raw_names if t in vocab}

    result.checked = names(raw.checked)
    result.optional = names(raw.optional) - result.checked
    result.mentioned = names(raw.mentioned)
    result.warning_keys = raw.warning_keys
    return result
