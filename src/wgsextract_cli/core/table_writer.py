"""Tables written as TSV or Parquet, the same way by every command.

TSV is the default and stays byte-for-byte what each command wrote before.
``--format parquet`` writes typed, zstd-compressed columns, row group by row
group, with the run's provenance in the file footer's key-value metadata.
Parquet needs the optional ``pyarrow`` package, which isn't installed for
users: a command asked for Parquet without it stops with a clear error.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, cast

from wgsextract_cli import __version__
from wgsextract_cli.core.atomic_output import atomic_output
from wgsextract_cli.core.paths import PathInput
from wgsextract_cli.core.results import current_result, fingerprint
from wgsextract_cli.core.utils import MissingDependencyError

FORMATS = ("tsv", "parquet")
SUFFIXES = {"tsv": ".tsv", "parquet": ".parquet"}
# Rows per Parquet row group, and so the most rows held in memory at once.
ROW_GROUP_ROWS = 128 * 1024
_ARROW_TYPES = {
    "TEXT": "string",
    "INTEGER": "int64",
    "REAL": "float64",
    "BOOLEAN": "bool",
}

Value = str | int | float | None


@dataclass(frozen=True)
class Column:
    name: str
    # TEXT, INTEGER, REAL or BOOLEAN: Parquet string, int64, double or boolean
    type: str
    description: str = ""
    spec: str = ""  # format() spec of the TSV text, e.g. ".2f"

    def text(self, value: Value) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return format(value, self.spec) if self.spec else str(value)


def tsv_lines(columns: Sequence[Column], rows: Iterable[Sequence[Value]]) -> str:
    """``rows`` as TSV text with a header line, as a TSV table file holds them."""
    lines = ["\t".join(c.name for c in columns)]
    lines += [
        "\t".join([c.text(v) for c, v in zip(columns, row, strict=True)])
        for row in rows
    ]
    return "\n".join(lines) + "\n"


def require_pyarrow() -> ModuleType:
    """``pyarrow.parquet``, or a MissingDependencyError saying how to get it."""
    try:
        import pyarrow.parquet as pq
    except ImportError as e:
        raise MissingDependencyError(
            "--format parquet needs the optional 'pyarrow' package, which is not "
            f"installed for {sys.executable}. Install it into that environment "
            "(pip install 'wgsextract-cli[parquet]', or pixi run -e parquet from "
            "a source checkout), or use --format tsv. pyarrow has no Windows "
            f"ARM64 build yet. ({e})"
        ) from e
    return cast(ModuleType, pq)


def table_format_type(hint: str = "") -> Callable[[str], str]:
    """The argparse type of ``--format``; ``hint`` is appended to the error for
    any other value."""

    def table_format(value: str) -> str:
        if value not in FORMATS:
            raise argparse.ArgumentTypeError(
                f"invalid choice: {value!r} (choose from {', '.join(FORMATS)}). {hint}".strip()
            )
        return value

    return table_format


def add_format_argument(
    parser: argparse.ArgumentParser,
    *,
    default: str | None = "tsv",
    help: str = "Table format: tsv, or parquet (needs pyarrow)",
) -> None:
    """Add ``--format`` (dest ``table_format``)."""
    parser.add_argument(
        "--format",
        dest="table_format",
        type=table_format_type(),
        choices=FORMATS,
        default=default,
        help=f"{help} (default: {default or 'none'})",
    )


def arrow_schema(
    columns: Sequence[Column], metadata: Mapping[str, str] | None = None
) -> Any:
    """The pyarrow schema of ``columns``, with each description as field metadata."""
    require_pyarrow()
    import pyarrow as pa

    return pa.schema(
        [
            pa.field(
                c.name,
                _ARROW_TYPES[c.type],
                metadata={"description": c.description} if c.description else None,
            )
            for c in columns
        ],
        metadata=dict(metadata or {}),
    )


def check_format(table_format: str | None) -> None:
    """Fail before any work when the chosen format can't be written."""
    if table_format == "parquet":
        require_pyarrow()


def table_path(stem: PathInput, table_format: str) -> str:
    return f"{stem}{SUFFIXES[table_format]}"


def provenance(
    inputs: Sequence[PathInput] = (),
    *,
    reference_build: str | None = None,
    sorted_by: str | None = None,
    **extra: str | None,
) -> dict[str, str]:
    """The Parquet footer metadata every command records: tool version, run
    time, the command and its redacted command line (or, from the Python API,
    its parameters), each input and its fingerprint, the reference build and
    the row order."""
    result = current_result()
    metadata = {
        "wgsextract_version": __version__,
        "created": datetime.now(timezone.utc).isoformat(),
        "command": result.command,
    }
    if result.provenance.argv:
        metadata["command_line"] = shlex.join(["wgsextract", *result.provenance.argv])
    elif result.provenance.params:
        metadata["params"] = json.dumps(result.provenance.params, default=str)
    for number, path in enumerate(inputs, start=1):
        key = "input" if number == 1 else f"input_{number}"
        metadata[key] = str(Path(path).absolute())
        metadata[f"{key}_fingerprint"] = fingerprint(path) or ""
    for key, value in (
        ("reference_build", reference_build),
        ("sorted_by", sorted_by),
        *extra.items(),
    ):
        if value is not None:
            metadata[key] = value
    return metadata


class TableWriter:
    """Write rows (sequences in ``columns`` order) to ``path`` as TSV or Parquet.

    Rows are buffered one row group at a time, so memory doesn't grow with the
    table. The file appears under its final name only when the ``with`` block
    completes without an error.
    """

    def __init__(
        self,
        path: PathInput,
        columns: Sequence[Column],
        table_format: str = "tsv",
        *,
        metadata: Mapping[str, str] | None = None,
        row_group_rows: int = ROW_GROUP_ROWS,
    ) -> None:
        if table_format not in FORMATS:
            raise ValueError(f"Unknown table format: {table_format}")
        self.path = str(path)
        self.columns = tuple(columns)
        self.format = table_format
        self.metadata = dict(metadata or {})
        self.row_group_rows = row_group_rows
        self.rows = 0
        self._buffer: list[Sequence[Value]] = []
        self._arrow: list[Any] = []
        self._arrow_rows = 0
        self._stack = ExitStack()
        self._tsv: Any = None
        self._parquet: Any = None
        self._schema: Any = None

    def __enter__(self) -> TableWriter:
        with ExitStack() as stack:
            staged = stack.enter_context(atomic_output(self.path))
            if self.format == "tsv":
                self._tsv = stack.enter_context(
                    open(staged, "w", newline="\n", encoding="utf-8")
                )
                self._tsv.write("\t".join(c.name for c in self.columns) + "\n")
            else:
                self._open_parquet(staged, stack)
            self._stack = stack.pop_all()
        return self

    def _open_parquet(self, staged: str, stack: ExitStack) -> None:
        pq = require_pyarrow()
        self._schema = arrow_schema(self.columns, self.metadata)
        self._parquet = pq.ParquetWriter(
            staged, self._schema, compression="zstd", write_statistics=True
        )
        stack.callback(self._parquet.close)

    def write(self, rows: Iterable[Sequence[Value]]) -> None:
        self._flush_arrow(final=True)
        for row in rows:
            self._buffer.append(row)
            if len(self._buffer) >= self.row_group_rows:
                self._flush_rows()

    def write_arrow(self, batch: Any) -> None:
        """Append a pyarrow RecordBatch or Table holding these columns, in order
        (Parquet only), for inputs pyarrow parses itself. Small batches are
        gathered into full row groups."""
        if self._parquet is None:
            raise ValueError("write_arrow needs a Parquet table")
        import pyarrow as pa

        self._flush_rows()
        table = batch if isinstance(batch, pa.Table) else pa.Table.from_batches([batch])
        self._arrow.append(table.rename_columns(self._schema.names).cast(self._schema))
        self._arrow_rows += table.num_rows
        if self._arrow_rows >= self.row_group_rows:
            self._flush_arrow(final=False)

    def _flush_arrow(self, *, final: bool) -> None:
        if not self._arrow:
            return
        import pyarrow as pa

        table = pa.concat_tables(self._arrow)
        whole = (
            table.num_rows
            if final
            else table.num_rows // self.row_group_rows * self.row_group_rows
        )
        self._parquet.write_table(
            table.slice(0, whole), row_group_size=self.row_group_rows
        )
        self.rows += whole
        rest = table.slice(whole)
        self._arrow = [rest] if rest.num_rows else []
        self._arrow_rows = rest.num_rows

    def _flush(self) -> None:
        self._flush_arrow(final=True)
        self._flush_rows()

    def _flush_rows(self) -> None:
        if not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        if self._tsv is not None:
            columns = self.columns
            self._tsv.writelines(
                "\t".join([c.text(v) for c, v in zip(columns, row, strict=True)]) + "\n"
                for row in batch
            )
        else:
            import pyarrow as pa

            arrays = [
                pa.array(values, type=field.type)
                for values, field in zip(
                    zip(*batch, strict=True), self._schema, strict=True
                )
            ]
            self._parquet.write_table(pa.Table.from_arrays(arrays, schema=self._schema))
        self.rows += len(batch)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc is None:
            try:
                self._flush()
            except BaseException as error:
                self._stack.__exit__(type(error), error, error.__traceback__)
                raise
        self._stack.__exit__(exc_type, exc, traceback)


def write_table(
    path: PathInput,
    columns: Sequence[Column],
    rows: Iterable[Sequence[Value]],
    table_format: str = "tsv",
    *,
    metadata: Mapping[str, str] | None = None,
) -> int:
    """Write ``rows`` to ``path``; return how many were written."""
    with TableWriter(path, columns, table_format, metadata=metadata) as writer:
        writer.write(rows)
    return writer.rows
