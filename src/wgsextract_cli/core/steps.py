"""Step-level resume for long multi-step workflows.

A workflow (realign, DeepVariant, analyze comprehensive) runs as named steps.
Each step declares its inputs, outputs, params and tools. When a step
finishes, the step journal (``<output>.steps.json``, beside the workflow's
main output) records:

- a key from the step's params, tool versions and definition version
- each input's fingerprint and, when an earlier step made it, that step's run
- each output's size and fingerprint

On a later run the step is skipped when its key matches, its inputs are
unchanged, the steps that made them haven't rerun, and every output is still
the one it wrote. Otherwise it reruns, and the log says why. A step is marked
``running`` before it starts and ``complete`` only after its outputs are
written. A run killed mid-step therefore never leaves a step that looks done.

``--force`` reruns every step and ``--force-step NAME`` one step (and so the
steps that use its outputs). ``--dry-run`` prints the plan without running
anything. The journal is held under a lock for the whole run, so two runs
can't write one output at once.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

from wgsextract_cli.core.paths import PathInput, absolute_path
from wgsextract_cli.core.utils import WGSExtractError

SCHEMA = "wgsextract.steps/1"
JOURNAL_SUFFIX = ".steps.json"


@dataclass(frozen=True)
class ResumePolicy:
    """How a workflow treats the steps an earlier run completed."""

    restart: bool = False
    force_steps: frozenset[str] = frozenset()
    dry_run: bool = False

    def forced(self, step: str) -> str | None:
        if self.restart:
            return "--force reruns every step"
        if step in self.force_steps:
            return "--force-step"
        return None


def add_resume_arguments(parser: argparse.ArgumentParser, steps: Sequence[str]) -> None:
    """The --force, --force-step and --dry-run options of a step workflow."""
    group = parser.add_argument_group(
        "resuming",
        "Completed steps are recorded in <output>.steps.json and skipped when "
        "a rerun's inputs, settings and tools are unchanged.",
    )
    group.add_argument(
        "--force",
        "--no-resume",
        dest="restart",
        action="store_true",
        help="Rerun every step, ignoring the steps an earlier run completed.",
    )
    group.add_argument(
        "--force-step",
        dest="force_steps",
        action="append",
        choices=list(steps),
        metavar="STEP",
        help="Rerun STEP and the steps that use its outputs (repeatable). "
        f"Steps: {', '.join(steps)}.",
    )
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Print which steps would run or be skipped, and why, then exit.",
    )


def resume_policy(args: argparse.Namespace) -> ResumePolicy:
    return ResumePolicy(
        restart=bool(getattr(args, "restart", False)),
        force_steps=frozenset(getattr(args, "force_steps", None) or ()),
        dry_run=bool(getattr(args, "dry_run", False)),
    )


@dataclass(frozen=True)
class Step:
    """One step of a workflow.

    ``inputs`` maps a role to the file it reads. ``outputs`` are the files the
    step must write; ``StepJournal.done`` can add outputs only known once
    the step has run. ``params`` and ``tools`` (whose versions are probed)
    shape the result. Leave out settings that don't, such as threads and
    memory. Bump ``version`` when the step's own logic changes its output.
    """

    name: str
    inputs: Mapping[str, PathInput | None] = field(default_factory=dict)
    outputs: Sequence[PathInput] = ()
    params: Mapping[str, Any] = field(default_factory=dict)
    tools: Sequence[str] = ()
    version: int = 1


@dataclass
class Decision:
    """Whether a step runs and why. ``partial`` says an earlier attempt with
    the same key was interrupted, so the step's own resumable work (such as
    DeepVariant's finished make_examples shards) may be kept."""

    step: Step
    run: bool
    reason: str
    key: str
    record: dict[str, Any] | None = None
    partial: bool = False

    @property
    def metrics(self) -> dict[str, Any]:
        return dict((self.record or {}).get("metrics") or {})

    @property
    def outputs(self) -> list[dict[str, Any]]:
        return list((self.record or {}).get("outputs") or [])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_json_value(v) for v in value]
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _tool_versions(tools: Sequence[str]) -> dict[str, str | None]:
    from wgsextract_cli.core.results import tool_version

    return {tool: tool_version(tool) for tool in sorted(set(tools))}


def _fingerprint(path: Path) -> str | None:
    from wgsextract_cli.core.sample_workspace import file_fingerprint

    return file_fingerprint(path)


def _report(message: str) -> None:
    from wgsextract_cli.api._runtime import report

    report("step", message)


def remove_partials(path: PathInput) -> None:
    """Delete the staged ``.<name>.<id>.partial<suffix>`` copies of ``path``
    that core.atomic_output left behind when a run was killed."""
    final = Path(path)
    suffix = "".join(final.suffixes)
    base = final.name[: -len(suffix)] if suffix else final.name
    if not final.parent.is_dir():
        return
    for stale in final.parent.glob(f".{base}.*.partial{suffix}*"):
        logging.info(f"Removing {stale.name}, left by an interrupted run.")
        with contextlib.suppress(OSError):
            stale.unlink()


class StepJournal:
    """The step records of one workflow output; see the module docstring.

    Use as a context manager, then for each step::

        decision = journal.decide(Step("align", inputs=..., outputs=[bam]))
        if decision.run and not journal.dry_run:
            with journal.running(decision):
                ...  # write the outputs
    """

    def __init__(self, path: PathInput, workflow: str, policy: ResumePolicy):
        self.path = absolute_path(path)
        self.workflow = workflow
        self.policy = policy
        self.plan: list[dict[str, str]] = []
        self._steps: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, str] = {}  # output path -> step rerunning it
        self._lock = contextlib.ExitStack()

    @classmethod
    def for_output(
        cls, output: PathInput, workflow: str, policy: ResumePolicy
    ) -> StepJournal:
        return cls(Path(str(output) + JOURNAL_SUFFIX), workflow, policy)

    @property
    def dry_run(self) -> bool:
        return self.policy.dry_run

    def recorded(self, step: str) -> bool:
        """Whether any run, finished or not, has recorded ``step``."""
        return step in self._steps

    # --- lifecycle -------------------------------------------------------

    def __enter__(self) -> StepJournal:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.dry_run:
            from wgsextract_cli.core.file_lock import build_lock

            lock = str(self.path.parent / f".{self.path.name}.lock")
            try:
                self._lock.enter_context(build_lock(lock, timeout=0))
            except WGSExtractError:
                raise WGSExtractError(
                    f"Another wgsextract run is writing {self.path.parent}: its "
                    f"lock is {lock}. Wait for it to finish, or delete the lock "
                    "file if no such run exists."
                ) from None
        self._steps = self._load()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self._summarise()
        finally:
            self._lock.close()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            logging.warning(f"Ignoring unreadable step journal {self.path}: {error}")
            return {}
        if (
            not isinstance(document, dict)
            or document.get("schema") != SCHEMA
            or document.get("workflow") != self.workflow
            or not isinstance(document.get("steps"), dict)
        ):
            logging.warning(f"Ignoring step journal {self.path}: not a {SCHEMA} file.")
            return {}
        return {
            name: record
            for name, record in document["steps"].items()
            if isinstance(record, dict)
        }

    def _save(self) -> None:
        from wgsextract_cli.core.atomic_output import atomic_write

        document = {"schema": SCHEMA, "workflow": self.workflow, "steps": self._steps}
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        atomic_write(self.path, text.encode("utf-8"))

    def _summarise(self) -> None:
        if not self.plan:
            return
        from wgsextract_cli.core.results import current_result, human_output

        result = current_result()
        ran = sum(1 for item in self.plan if item["action"] == "run")
        result.add_metrics(
            steps_run=ran,
            steps_skipped=len(self.plan) - ran,
        )
        result.set_details(
            {**(result.details or {}), "steps": self.plan, "dry_run": self.dry_run}
        )
        if self.path.is_file() and not self.dry_run:
            result.add_output(self.path, "step_journal", kind="json")
        if self.dry_run:
            width = max(len(item["step"]) for item in self.plan)
            lines = [f"{self.workflow} plan ({self.path.name}):"] + [
                f"  {item['step']:<{width}}  {item['action']:<4}  {item['reason']}"
                for item in self.plan
            ]
            human_output("\n".join(lines))

    # --- identities ------------------------------------------------------

    def _stored_path(self, path: PathInput) -> str:
        resolved = absolute_path(path)
        try:
            return resolved.relative_to(self.path.parent).as_posix()
        except ValueError:
            return str(resolved)

    def _resolve(self, stored: str) -> Path:
        path = Path(stored)
        return path if path.is_absolute() else self.path.parent / path

    def _producer(self, path: Path) -> str | None:
        """The complete step that wrote ``path``, if any."""
        for name, record in self._steps.items():
            if record.get("status") != "complete":
                continue
            for output in record.get("outputs") or []:
                if self._resolve(output["path"]) == path:
                    return name
        return None

    def _input(self, role: str, path: PathInput) -> dict[str, Any]:
        resolved = absolute_path(path)
        item: dict[str, Any] = {"role": role, "path": str(resolved)}
        producer = self._producer(resolved)
        if producer:
            item["from_step"] = producer
            item["from_run"] = self._steps[producer].get("run_id")
        if resolved.is_file():
            item["size"] = resolved.stat().st_size
            item["fingerprint"] = _fingerprint(resolved)
        elif resolved.is_dir():
            item["kind"] = "directory"
        else:
            item["missing"] = True
        return item

    def _key(self, step: Step, tools: Mapping[str, str | None]) -> str:
        payload = {
            "workflow": self.workflow,
            "step": step.name,
            "version": step.version,
            "params": _json_value(step.params),
            "tools": tools,
        }
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    # --- deciding --------------------------------------------------------

    def check(self, step: Step, *, after: Sequence[Decision] = ()) -> Decision:
        """Whether ``step`` must run, without logging or recording anything."""
        tools = _tool_versions(step.tools)
        key = self._key(step, tools)
        record = self._steps.get(step.name)

        def run(reason: str, partial: bool = False) -> Decision:
            return Decision(step, True, reason, key, record, partial)

        if forced := self.policy.forced(step.name):
            return run(forced)
        if record is None:
            return run("not run before")
        for earlier in after:
            if earlier.run:
                return run(f"step {earlier.step.name} runs before it")
        inputs = {
            role: self._input(role, path)
            for role, path in step.inputs.items()
            if path is not None
        }
        for role, item in inputs.items():
            if item["path"] in self._pending:
                return run(f"step {self._pending[item['path']]} rebuilds its {role}")
        if record.get("status") != "complete":
            same = record.get("key") == key and self._same_inputs(record, inputs)
            return run("an earlier run was interrupted during it", partial=same)
        if record.get("key") != key:
            return run(self._key_change(step, record, tools))
        if changed := self._changed_input(record, inputs):
            return run(changed)
        if stale := self._stale_output(step, record):
            return run(stale)
        finished = record.get("finished", "an earlier run")
        return Decision(step, False, f"complete ({finished})", key, record)

    def _same_inputs(
        self, record: Mapping[str, Any], inputs: Mapping[str, Mapping[str, Any]]
    ) -> bool:
        recorded = {item.get("role"): item for item in record.get("inputs") or []}
        return all(
            recorded.get(role, {}).get("fingerprint") == item.get("fingerprint")
            for role, item in inputs.items()
        )

    def _key_change(
        self, step: Step, record: Mapping[str, Any], tools: Mapping[str, Any]
    ) -> str:
        if record.get("version") != step.version:
            return "the step's definition changed in this wgsextract version"
        old_params = record.get("params") or {}
        params = _json_value(step.params)
        changed = sorted(
            name
            for name in set(old_params) | set(params)
            if old_params.get(name) != params.get(name)
        )
        if changed:
            return "settings changed: " + ", ".join(
                f"{name} {old_params.get(name)!r} -> {params.get(name)!r}"
                for name in changed
            )
        old_tools = record.get("tools") or {}
        for tool, version in tools.items():
            if old_tools.get(tool) != version:
                return f"{tool} changed: {old_tools.get(tool)} -> {version}"
        return "its settings changed"

    def _changed_input(
        self, record: Mapping[str, Any], inputs: Mapping[str, Mapping[str, Any]]
    ) -> str | None:
        recorded = {item.get("role"): item for item in record.get("inputs") or []}
        for role in sorted(set(recorded) - set(inputs)):
            if recorded[role].get("declared", True):
                return f"it no longer reads {role}"
        for role, item in inputs.items():
            old = recorded.get(role)
            if old is None:
                return f"it now reads {role} ({item['path']})"
            if item.get("missing"):
                return f"its {role} {item['path']} is missing"
            if item.get("from_step") and item.get("from_run") != old.get("from_run"):
                return f"step {item['from_step']} rebuilt its {role}"
            if item.get("fingerprint") != old.get("fingerprint"):
                return f"its {role} {item['path']} changed"
        # Inputs a step reported only once it ran (a child command's data files).
        for role, old in recorded.items():
            if role in inputs or old.get("declared", True):
                continue
            path = Path(old["path"])
            if not path.is_file() or _fingerprint(path) != old.get("fingerprint"):
                return f"its {role} {path} changed"
        return None

    def _stale_output(self, step: Step, record: Mapping[str, Any]) -> str | None:
        outputs = {
            str(self._resolve(item["path"])): item
            for item in record.get("outputs") or []
        }
        for path in step.outputs:
            if str(absolute_path(path)) not in outputs:
                return f"it didn't record {path}"
        for name, item in outputs.items():
            path = Path(name)
            if item.get("retired"):
                return f"{path.name} was removed after use"
            if not path.exists():
                return f"{path.name} is missing"
            if item.get("kind") == "directory" or path.is_dir():
                continue
            size = path.stat().st_size
            if size != item.get("size"):
                return (
                    f"{path.name} changed size ({item.get('size')} -> {size} bytes; "
                    "truncated or rewritten)"
                )
            if _fingerprint(path) != item.get("fingerprint"):
                return f"{path.name} changed since the step wrote it"
        return None

    def decide(self, step: Step, *, after: Sequence[Decision] = ()) -> Decision:
        """``check`` the step, then log and record the decision.

        ``after``: decisions of earlier steps whose rerun reruns this one,
        for inputs not named in ``inputs`` (realign's aligner reads the
        FASTQs it extracted, but records the BAM they came from).
        """
        decision = self.check(step, after=after)
        action = "run" if decision.run else "skip"
        self.plan.append(
            {"step": step.name, "action": action, "reason": decision.reason}
        )
        if decision.run:
            for path in step.outputs:
                self._pending[str(absolute_path(path))] = step.name
            verb = "Would run" if self.dry_run else "Running"
            message = f"{verb} step {step.name}: {decision.reason}."
        else:
            message = f"Skipping step {step.name}: {decision.reason}."
        logging.info(message)
        _report(message)
        return decision

    # --- recording -------------------------------------------------------

    @contextlib.contextmanager
    def running(
        self, decision: Decision, *, keep_partial: bool = False
    ) -> Iterator[StepRun]:
        """Mark the step running, yield, then record it complete. Stale
        staged outputs of an interrupted attempt are removed first."""
        if self.dry_run:
            raise WGSExtractError(f"Step {decision.step.name} can't run in --dry-run.")
        step = decision.step
        if not keep_partial:
            for path in step.outputs:
                remove_partials(path)
        self._steps[step.name] = {
            "status": "running",
            "key": decision.key,
            "started": _now(),
            "inputs": [
                {**self._input(role, path), "declared": True}
                for role, path in step.inputs.items()
                if path is not None
            ],
        }
        self._save()
        handle = StepRun()
        clock = time.monotonic()
        yield handle
        self._complete(decision, handle, time.monotonic() - clock)

    def _complete(self, decision: Decision, handle: StepRun, seconds: float) -> None:
        step = decision.step
        outputs: list[dict[str, Any]] = []
        for path, extra in [(p, {}) for p in step.outputs] + handle.outputs:
            resolved = absolute_path(path)
            if not resolved.exists():
                raise WGSExtractError(
                    f"Step {step.name} finished without writing {resolved}."
                )
            item: dict[str, Any] = {"path": self._stored_path(resolved), **extra}
            if resolved.is_dir():
                item["kind"] = "directory"
            else:
                item["size"] = resolved.stat().st_size
                item["fingerprint"] = _fingerprint(resolved)
            outputs.append(item)
        inputs = [
            {**self._input(role, path), "declared": True}
            for role, path in step.inputs.items()
            if path is not None
        ] + [
            {**self._input(role, path), "declared": False}
            for role, path in handle.inputs
        ]
        self._steps[step.name] = {
            "status": "complete",
            "key": decision.key,
            "version": step.version,
            "params": _json_value(step.params),
            "tools": _tool_versions(step.tools),
            "inputs": inputs,
            "outputs": outputs,
            "metrics": _json_value(handle.metrics),
            "run_id": uuid.uuid4().hex,
            "started": self._steps.get(step.name, {}).get("started", _now()),
            "finished": _now(),
            "seconds": round(seconds, 1),
        }
        self._save()
        logging.info(f"Step {step.name} complete in {seconds:.0f} s.")

    def retire(self, step: str, paths: Sequence[PathInput]) -> None:
        """Note that ``step``'s outputs ``paths`` were deleted on purpose
        after use, so later steps can still vouch for what they read."""
        record = self._steps.get(step)
        if not record or self.dry_run:
            return
        wanted = {str(absolute_path(p)) for p in paths}
        for output in record.get("outputs") or []:
            if str(self._resolve(output["path"])) in wanted:
                output["retired"] = True
        self._save()


@dataclass
class StepRun:
    """What a running step reports beyond its declared outputs."""

    outputs: list[tuple[PathInput, dict[str, Any]]] = field(default_factory=list)
    inputs: list[tuple[str, PathInput]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def add_output(self, path: PathInput, **details: Any) -> None:
        self.outputs.append((path, _json_value(details)))

    def add_input(self, role: str, path: PathInput) -> None:
        self.inputs.append((role, path))
