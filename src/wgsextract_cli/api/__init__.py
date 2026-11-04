"""The typed Python API of WGS Extract.

Every operation takes a frozen ``*Params`` dataclass and returns a
``Result`` (the ``wgsextract.result/1`` manifest model the CLI writes)::

    from pathlib import Path
    from wgsextract_cli import api

    res = api.vcf.snp(api.VcfSnpParams(input=Path("x.cram"), region="chrM"))
    res.outputs, res.metrics, res.warnings

Operations raise ``CommandFailed`` (carrying the failed ``Result``) unless
called with ``check=False``. They accept a ``progress`` callback and a
``CancelToken``. Operations run one at a time per process. They write the same
output files and manifests as the CLI but print nothing to stdout: their
human-readable text is suppressed and external tools' stdout goes to stderr.
Stdout itself is left alone, so the caller's own writes (including from a
progress callback or other threads) still reach it, e.g. a stdio JSON-RPC server.

Unset fields with a config.toml setting take it, as on the CLI. The user's
config.toml is loaded on the first call; ``use_config(path)`` selects another.

The CLI is built on this layer: each migrated command's options are generated
from its Params fields, so the API and ``--help`` cannot drift.

Stability: the names exported here and the group modules (``api.info``,
``api.vcf``, ``api.lineage``, ``api.microarray``) follow semantic versioning
from the 1.0 release; adding fields with defaults is not a breaking change.
Modules whose names start with ``_`` are private and may change at any time.
See docs/api.md.
"""

from wgsextract_cli.core.results import (
    CommandResult as Result,
)
from wgsextract_cli.core.results import (
    FileRecord,
    ResultWarning,
)
from wgsextract_cli.core.utils import (
    MissingDependencyError,
    UsageError,
    WGSExtractError,
)

from . import info, lineage, microarray, vcf
from ._params import Params, json_schema
from ._runtime import (
    Cancelled,
    CancelToken,
    CommandFailed,
    ProgressCallback,
    ProgressEvent,
    use_config,
)
from .info import CalculateCoverageParams, ChipParams, CoverageSampleParams, InfoParams
from .lineage import MtHaplogroupParams, YHaplogroupParams, YStrParams
from .microarray import MicroarrayParams
from .vcf import VcfIndelParams, VcfSnpParams

__all__ = [
    "CalculateCoverageParams",
    "CancelToken",
    "Cancelled",
    "ChipParams",
    "CommandFailed",
    "CoverageSampleParams",
    "FileRecord",
    "InfoParams",
    "MicroarrayParams",
    "MissingDependencyError",
    "MtHaplogroupParams",
    "Params",
    "ProgressCallback",
    "ProgressEvent",
    "Result",
    "ResultWarning",
    "UsageError",
    "VcfIndelParams",
    "VcfSnpParams",
    "WGSExtractError",
    "YHaplogroupParams",
    "YStrParams",
    "info",
    "json_schema",
    "lineage",
    "microarray",
    "use_config",
    "vcf",
]
