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
PROBES = {"get_tool_path"}


def tool_vocabulary() -> dict[str, str]:
    """Tool names a check can mention, mapped to the name users see.

    Pixi environment launchers such as ``pharmcat_python`` map to their
    environment (``pharmcat``), which is what ``deps check`` reports.
    """
    names = [
        *tool_catalog.MANDATORY_TOOLS,
        *tool_catalog.OPTIONAL_TOOLS,
        *tool_catalog.PIXI_TOOL_ENVS,
        "docker",
    ]
    vocab = {name: name for name in names}
    for name, env in tool_catalog.PIXI_TOOL_ENVS.items():
        if name.endswith("_python"):
            vocab[name] = env
    return vocab


@dataclass
class Scan:
    checked: set[str] = field(default_factory=set)
    probed: set[str] = field(default_factory=set)
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


def _strings(expr: ast.AST, func: types.FunctionType, body: ast.AST, depth=0):
    """String values an expression can take: literals, globals, local assignments."""
    found: set[str] = set()
    scope = func.__globals__
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
                        found |= _strings(assign.value, func, body, depth + 1)
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
        if (
            isinstance(node, ast.Call)
            and _call_name(node) == "verify_dependencies"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in params
        ):
            return params.index(node.args[0].id)
    return None


def _argument(node: ast.Call, target: types.FunctionType, index: int):
    if index < len(node.args):
        return node.args[index]
    definition = _definition(target)
    name = _params(definition)[index] if definition is not None else None
    return next((k.value for k in node.keywords if k.arg == name), None)


def scan(handler: Callable[..., Any]) -> Scan:
    """Tools ``handler`` checks for (or probes), in user-facing names."""
    result = Scan()
    raw = Scan()
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
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if node.args:
                if name in CHECKS:
                    raw.checked |= _strings(node.args[0], func, body)
                elif name in PROBES:
                    raw.probed |= _strings(node.args[0], func, body)
                elif name == "print_warning":
                    raw.warning_keys |= _strings(node.args[0], func, body)
            target = _resolve(node.func, scope)
            if isinstance(target, types.FunctionType):
                queue.append(target)
                index = _forwarded_param(target)
                if index is not None:
                    expr = _argument(node, target, index)
                    if expr is not None:
                        raw.checked |= _strings(expr, func, body)
            for arg in node.args:
                callback = _resolve(arg, scope)
                if isinstance(callback, types.FunctionType):
                    queue.append(callback)
    vocab = tool_vocabulary()
    result.checked = {vocab[t] for t in raw.checked if t in vocab}
    result.probed = {vocab[t] for t in raw.probed if t in vocab} - result.checked
    result.warning_keys = raw.warning_keys
    return result
