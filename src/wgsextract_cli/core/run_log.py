"""Opt-in, exclusive run logs and small provenance files."""

import argparse
import hashlib
import json
import logging
import os
import platform
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from wgsextract_cli import __version__

_active: "RunLog | None" = None


class RunLogError(OSError):
    """A failure creating or finalizing an explicitly requested run log."""


def active_run_log() -> bool:
    return _active is not None


def is_run_log_path(path: Path) -> bool:
    return bool(
        _active
        and path.resolve() in {_active.path.resolve(), _active.provenance.resolve()}
    )


def record_command(command: Sequence[str]) -> None:
    if _active:
        _active.record(command)


class RunLog:
    def __init__(self, path: Path) -> None:
        self.commands: list[list[str]] = []
        self._lock = threading.Lock()
        self.path = path
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.provenance = Path(str(path) + ".provenance.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Never truncate an earlier run or its provenance.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.stream = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        try:
            os.close(
                os.open(self.provenance, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            )
        except OSError:
            self.stream.close()
            path.unlink()
            raise
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )

    def record(self, command: Sequence[str]) -> None:
        from wgsextract_cli.core.utils import WGSExtractError

        reserved = {self.path.resolve(), self.provenance.resolve()}
        for arg in command[1:]:
            if arg == "-":
                continue
            try:
                collision = Path(arg).expanduser().resolve() in reserved
            except (OSError, ValueError):
                collision = False
            if collision:
                raise WGSExtractError(
                    "Persistent run log collides with a tool input/output; choose a separate log path."
                )
        with self._lock:
            self.commands.append(list(command))

    def write_metadata(self, data: dict[str, object]) -> None:
        temporary = self.provenance.with_name(
            self.provenance.name + f".{uuid4().hex}.tmp"
        )
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(data, stream, indent=2)
                stream.write("\n")
            temporary.replace(self.provenance)
        finally:
            temporary.unlink(missing_ok=True)

    def metadata(
        self, args: argparse.Namespace, argv: Sequence[str], status: str
    ) -> dict[str, object]:
        from wgsextract_cli.core.dependency_checks import get_tool_version

        tools = set()
        for command in self.commands:
            if command:
                tools.add(Path(command[0]).stem)
            tools.update(
                tool
                for tool in (
                    "bwa",
                    "bwa-mem2",
                    "samtools",
                    "bcftools",
                    "gatk",
                    "minimap2",
                    "samblaster",
                )
                if tool in command
            )
            if "org.broadinstitute.hellbender.Main" in command:
                tools.add("gatk")
        versions = {
            tool: platform.python_version()
            if tool.startswith("python")
            else get_tool_version(tool)
            for tool in sorted(tools)
        }
        reference: dict[str, object] = {"path": getattr(args, "ref", None)}
        if reference["path"]:
            path = Path(str(reference["path"])).expanduser()
            if path.is_file():
                stat = path.stat()
                reference.update(size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
                indexes = {}
                for index in (Path(str(path) + ".fai"), path.with_suffix(".dict")):
                    if index.is_file():
                        with index.open("rb") as stream:
                            digest = hashlib.sha256()
                            while chunk := stream.read(65536):
                                digest.update(chunk)
                            indexes[str(index)] = digest.hexdigest()
                reference["index_sha256"] = indexes
        return {
            "schema_version": 1,
            "argv": list(argv),
            "wgsextract_version": __version__,
            "platform": platform.platform(),
            "status": status,
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "tool_versions": versions,
            "commands": self.commands,
            "reference": reference,
        }


@contextmanager
def run_logging(args: argparse.Namespace, argv: Sequence[str]) -> Iterator[None]:
    global _active
    path = getattr(args, "log_file", None)
    directory = getattr(args, "log_dir", None)
    if not path and not directory:
        yield
        return
    if not path:
        name = (
            datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S")
            + f"-{uuid4().hex}.log"
        )
        path = str(Path(str(directory)).expanduser() / name)
    try:
        session = RunLog(Path(path).expanduser())
    except OSError as error:
        raise RunLogError(str(error)) from error
    logger = logging.getLogger()
    old_level = logger.level
    logger.addHandler(session.handler)
    logger.setLevel(logging.DEBUG)
    _active = session
    status = "failed"
    try:
        try:
            session.write_metadata(
                {
                    "schema_version": 1,
                    "argv": list(argv),
                    "wgsextract_version": __version__,
                    "started_at": session.started_at,
                    "status": "running",
                    "tool_versions": {},
                }
            )
        except OSError as error:
            raise RunLogError(str(error)) from error
        logging.info("Persistent run log: %s", session.path)
        yield
        status = "success"
    finally:
        _active = None
        try:
            data = session.metadata(args, argv, status)
            session.write_metadata(data)
        except Exception as error:
            if status == "success":
                raise RunLogError(str(error)) from error
            logging.warning("Could not finalize run provenance", exc_info=True)
        finally:
            logger.removeHandler(session.handler)
            session.handler.close()
            session.stream.close()
            logger.setLevel(old_level)
