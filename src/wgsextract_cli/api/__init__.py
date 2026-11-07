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
output files and manifests as the CLI but print nothing to stdout: the
human-readable report the CLI would print is in ``Result.text`` instead, and
external tools' stdout goes to stderr.
Stdout itself is left alone, so the caller's own writes (including from a
progress callback or other threads) still reach it, e.g. a stdio JSON-RPC server.

Unset fields with a config.toml setting take it, as on the CLI. The user's
config.toml is loaded on the first call; ``use_config(path)`` selects another.

The CLI is built on this layer: each migrated command's options are generated
from its Params fields, so the API and ``--help`` cannot drift.

Stability: the names exported here and the group modules (``api.extract``,
``api.info``, ``api.bam``, ``api.qc``, ``api.align``, ``api.vcf``,
``api.lineage``, ``api.microarray``, ``api.panel``, ``api.ref``,
``api.repair``, ``api.report``, ``api.sample``, ``api.support``, ``api.pet``, ``api.analyze``,
``api.vep``) follow
semantic versioning from the 1.0 release; adding fields with defaults is not
a breaking change.
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

from . import (
    align,
    analyze,
    bam,
    extract,
    info,
    lineage,
    microarray,
    panel,
    pet,
    qc,
    ref,
    repair,
    report,
    sample,
    support,
    vcf,
    vep,
)
from ._params import Params, json_schema
from ._runtime import (
    Cancelled,
    CancelToken,
    CommandFailed,
    ProgressCallback,
    ProgressEvent,
    use_config,
)
from .align import AlignParams, BasecallParams, RealignParams
from .analyze import AnalyzeBatchGenParams, AnalyzeComprehensiveParams
from .bam import (
    BamIdentifyParams,
    BamIndexParams,
    BamMergeParams,
    BamSortParams,
    BamToBamParams,
    BamToCramParams,
    BamToUbamParams,
    BamUnalignParams,
    BamUnindexParams,
    BamUnsortParams,
)
from .extract import (
    BamSubsetParams,
    CustomParams,
    MitoFastaParams,
    MitoVcfParams,
    MtBamParams,
    UnmappedParams,
    YdnaBamParams,
    YdnaVcfParams,
    YMtExtractParams,
)
from .info import CalculateCoverageParams, ChipParams, CoverageSampleParams, InfoParams
from .lineage import MtHaplogroupParams, YHaplogroupParams, YStrParams
from .microarray import MicroarrayParams
from .panel import PanelBedParams
from .pet import PetAlignParams
from .qc import (
    FakeDataParams,
    FastpParams,
    FastqcParams,
    MultiqcMetricsParams,
    MultiqcParams,
    QcVcfParams,
)
from .ref import (
    RefAadrParams,
    RefAlphamissenseParams,
    RefBootstrapParams,
    RefCaddParams,
    RefClinvarParams,
    RefCountNsParams,
    RefDownloadParams,
    RefGeneMapParams,
    RefGerpParams,
    RefGnomadParams,
    RefIndexParams,
    RefLibraryParams,
    RefPharmgkbParams,
    RefPhylopParams,
    RefRevelParams,
    RefSpliceaiParams,
    RefStatusParams,
    RefVerifyParams,
    RefYbrowseParams,
)
from .repair import RepairFtdnaBamParams, RepairFtdnaVcfParams
from .report import ReportParams
from .sample import SampleSetParams, SampleShowParams, SampleUnsetParams
from .support import IssueReport, ReportIssueParams
from .vcf import (
    VcfAlphamissenseParams,
    VcfAnnotateParams,
    VcfCaddParams,
    VcfChainAnnotateParams,
    VcfClair3Params,
    VcfClinvarParams,
    VcfCnvParams,
    VcfDeepvariantParams,
    VcfFilterParams,
    VcfFreebayesParams,
    VcfGatkParams,
    VcfGerpParams,
    VcfGnomadParams,
    VcfIndelParams,
    VcfPharmcatParams,
    VcfPharmgkbParams,
    VcfPhaseParams,
    VcfPhylopParams,
    VcfRevelParams,
    VcfSnpParams,
    VcfSpliceaiParams,
    VcfSvParams,
    VcfTableParams,
    VcfToFastaParams,
    VcfTrioParams,
)
from .vep import VepRunParams

__all__ = [
    "AlignParams",
    "AnalyzeBatchGenParams",
    "AnalyzeComprehensiveParams",
    "BasecallParams",
    "BamIdentifyParams",
    "BamIndexParams",
    "BamMergeParams",
    "BamSortParams",
    "BamSubsetParams",
    "BamToBamParams",
    "BamToCramParams",
    "BamToUbamParams",
    "BamUnalignParams",
    "BamUnindexParams",
    "BamUnsortParams",
    "CalculateCoverageParams",
    "CancelToken",
    "Cancelled",
    "ChipParams",
    "CommandFailed",
    "CoverageSampleParams",
    "CustomParams",
    "FakeDataParams",
    "FastpParams",
    "FastqcParams",
    "FileRecord",
    "InfoParams",
    "IssueReport",
    "MicroarrayParams",
    "MissingDependencyError",
    "MitoFastaParams",
    "MitoVcfParams",
    "MtBamParams",
    "MtHaplogroupParams",
    "PanelBedParams",
    "MultiqcMetricsParams",
    "MultiqcParams",
    "Params",
    "PetAlignParams",
    "ProgressCallback",
    "ProgressEvent",
    "QcVcfParams",
    "RealignParams",
    "RefAadrParams",
    "RefAlphamissenseParams",
    "RefBootstrapParams",
    "RefCaddParams",
    "RefClinvarParams",
    "RefCountNsParams",
    "RefDownloadParams",
    "RefGeneMapParams",
    "RefGerpParams",
    "RefGnomadParams",
    "RefIndexParams",
    "RefLibraryParams",
    "RefPharmgkbParams",
    "RefPhylopParams",
    "RefRevelParams",
    "RefSpliceaiParams",
    "RefStatusParams",
    "RefVerifyParams",
    "RefYbrowseParams",
    "RepairFtdnaBamParams",
    "RepairFtdnaVcfParams",
    "ReportIssueParams",
    "ReportParams",
    "Result",
    "ResultWarning",
    "SampleSetParams",
    "SampleShowParams",
    "SampleUnsetParams",
    "UnmappedParams",
    "UsageError",
    "VcfAlphamissenseParams",
    "VcfAnnotateParams",
    "VcfCaddParams",
    "VcfChainAnnotateParams",
    "VcfClair3Params",
    "VcfClinvarParams",
    "VcfCnvParams",
    "VcfDeepvariantParams",
    "VcfFilterParams",
    "VcfFreebayesParams",
    "VcfGatkParams",
    "VcfGerpParams",
    "VcfGnomadParams",
    "VcfIndelParams",
    "VcfPharmcatParams",
    "VcfPharmgkbParams",
    "VcfPhaseParams",
    "VcfPhylopParams",
    "VcfRevelParams",
    "VcfSnpParams",
    "VcfSpliceaiParams",
    "VcfSvParams",
    "VcfTableParams",
    "VcfToFastaParams",
    "VcfTrioParams",
    "VepRunParams",
    "WGSExtractError",
    "YHaplogroupParams",
    "YMtExtractParams",
    "YStrParams",
    "YdnaBamParams",
    "YdnaVcfParams",
    "align",
    "analyze",
    "bam",
    "extract",
    "info",
    "json_schema",
    "lineage",
    "microarray",
    "panel",
    "pet",
    "qc",
    "ref",
    "repair",
    "report",
    "sample",
    "support",
    "use_config",
    "vcf",
    "vep",
]
