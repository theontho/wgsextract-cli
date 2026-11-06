"""`basecall`: Nanopore POD5 signal to an unaligned BAM with ONT's Dorado.

Dorado is an optional tool that users install from ONT's releases; it is
checked, never installed. Dorado picks the model for the run's chemistry and
downloads it into the models folder (the wgsextract cache by default). The
unaligned BAM keeps Dorado's read tags, MM/ML base modifications included,
and `align --r1` takes it as Nanopore reads.
"""

import argparse
import logging
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api.align import DORADO_MODELS, BasecallParams
from wgsextract_cli.commands._extract_helpers import cleanup_files
from wgsextract_cli.core.atomic_output import atomic_outputs_enabled, safe_output
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.dev_download_cache import wgsextract_cache_root
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.results import current_result, human_output, warn
from wgsextract_cli.core.samtools_commands import alignment_index
from wgsextract_cli.core.tool_catalog import INSTALL_HINTS
from wgsextract_cli.core.utils import (
    MissingDependencyError,
    UsageError,
    WGSExtractError,
    popen,
    run_command,
)
from wgsextract_cli.core.variant_files import resolve_reference

DORADO = "dorado"
# 1.0 dropped FAST5 and settled the model-complex and download options used here.
MIN_DORADO = (1, 0)
INSTALL_HINT = INSTALL_HINTS[DORADO]
FAST5_HINT = (
    "Dorado reads POD5 only; convert FAST5 first with `pod5 convert fast5 "
    "<files> --output <folder>` (the pod5 package from PyPI or Bioconda)."
)
_DEVICE = re.compile(r"(auto|cpu|metal|cuda:(all|\d+(,\d+)*))")
_MODEL = re.compile(rf"({'|'.join(DORADO_MODELS)})(@(v\d+(\.\d+)*|latest))?")
_MODIFICATION = re.compile(r"[A-Za-z0-9_]+")


def models_directory(value: Path | str | None) -> Path:
    """Where Dorado keeps its models: the given folder, else the cache's."""
    if value:
        return Path(value).expanduser()
    return wgsextract_cache_root() / "dorado-models"


def resolve_dorado(explicit: str | None) -> tuple[str, str]:
    """The dorado executable and its version; MissingDependencyError, with how
    to install it, when it is missing or older than 1.0."""
    if not explicit:
        verify_dependencies([DORADO])
    path = explicit or get_tool_path(DORADO)
    # A bare name (no folder) is looked up like any other tool.
    if explicit and Path(explicit).name == explicit:
        path = get_tool_path(explicit)
    if not path:
        raise MissingDependencyError(INSTALL_HINT)
    if explicit and path == explicit and not Path(path).is_file():
        raise MissingDependencyError(
            f"--dorado-path {path} doesn't exist. {INSTALL_HINT}"
        )
    try:
        probe = run_command([path, "--version"], capture_output=True, check=False)
    except OSError as error:
        raise MissingDependencyError(f"{path} doesn't run: {error}") from error
    text = f"{probe.stdout}\n{probe.stderr}"
    match = re.search(r"(\d+)\.(\d+)(\.\d+)?", text)
    if probe.returncode or not match:
        raise MissingDependencyError(
            f"{path} --version failed; is it ONT's Dorado? {INSTALL_HINT}"
        )
    version = match.group(0)
    if (int(match.group(1)), int(match.group(2))) < MIN_DORADO:
        raise MissingDependencyError(
            f"Dorado {version} is too old; basecall needs Dorado 1.0 or newer. "
            + INSTALL_HINT
        )
    return path, version


def _nvidia_gpu() -> bool:
    smi = get_tool_path("nvidia-smi")
    if not smi:
        return False
    try:
        listing = run_command([smi, "-L"], capture_output=True, check=False)
    except OSError:
        return False
    return listing.returncode == 0 and "GPU " in listing.stdout


def resolve_device(requested: str) -> str:
    """Dorado's -x for ``requested``; auto picks Apple silicon's GPU, else an
    NVIDIA GPU that nvidia-smi lists, else the CPU."""
    if not _DEVICE.fullmatch(requested):
        raise UsageError(
            f"--device {requested}: choose auto, cpu, metal, cuda:all or "
            "cuda:<id>[,<id>...]."
        )
    apple_silicon = sys.platform == "darwin" and platform.machine() == "arm64"
    if requested == "metal" and not apple_silicon:
        raise UsageError("--device metal needs a Mac with Apple silicon.")
    if requested != "auto":
        return requested
    if apple_silicon:
        return "metal"
    return "cuda:all" if _nvidia_gpu() else "cpu"


def check_model(model: str) -> bool:
    """True when ``model`` is a model folder, False for a name Dorado resolves."""
    if _MODEL.fullmatch(model):
        return False
    if Path(model).expanduser().is_dir():
        return True
    raise UsageError(
        f"--model {model}: give fast, hac or sup (optionally @v<version>), or "
        "an existing model folder."
    )


def modification_codes(value: str | None) -> list[str]:
    codes = [code.strip() for code in (value or "").split(",") if code.strip()]
    for code in codes:
        if not _MODIFICATION.fullmatch(code):
            raise UsageError(f"--modified-bases {code}: not a modification code.")
    return codes


def pod5_files(source: Path) -> list[Path]:
    """The POD5 files ``source`` names, or a UsageError saying what is wrong."""
    if source.is_dir():
        files = sorted(source.rglob("*.pod5"))
        if files:
            return files
        if next(source.rglob("*.fast5"), None):
            raise UsageError(f"{source} holds FAST5 files only. {FAST5_HINT}")
        raise UsageError(f"{source} holds no .pod5 files.")
    if source.suffix.lower() == ".fast5":
        raise UsageError(f"{source} is a FAST5 file. {FAST5_HINT}")
    if source.suffix.lower() != ".pod5":
        raise UsageError(f"{source} isn't a POD5 file (.pod5) or a folder of them.")
    return [source]


def output_name(source: Path, model: str, codes: list[str], subset: bool) -> str:
    """<stem>.<model>[.<codes>][.subset].bam, so that a SUP re-basecall
    doesn't replace the HAC BAM it came from."""
    stem = source.name if source.is_dir() else source.stem
    tag = Path(model).name if Path(model).expanduser().is_dir() else model
    parts = [stem, re.sub(r"[^A-Za-z0-9._-]+", "-", tag), *codes]
    if subset:
        parts.append("subset")
    return ".".join(parts) + ".bam"


def panel_read_ids(alignment: str, bed: str, ref: str | None, out: str) -> int:
    """Write the IDs of the reads ``alignment`` places in ``bed``; reads that
    Dorado split are named by their parent (pi tag), the ID in the POD5."""
    cram = ["-T", ref] if ref else []
    # -M walks the index once per merged region instead of reading the file.
    multi = ["-M"] if alignment_index(alignment) else []
    command = ["samtools", "view", "-F", "0x4", *multi, "-L", bed, *cram, alignment]
    ids: set[str] = set()
    with tempfile.TemporaryFile("w+") as errors:
        process = popen(command, stdout=subprocess.PIPE, stderr=errors, text=True)
        assert process.stdout is not None
        with process.stdout:
            for line in process.stdout:
                fields = line.rstrip("\n").split("\t")
                parent = next((t[5:] for t in fields[11:] if t.startswith("pi:Z:")), "")
                ids.add(parent or fields[0])
        if process.wait():
            errors.seek(0)
            raise WGSExtractError(
                f"samtools couldn't read {alignment} in {bed}: {errors.read().strip()}"
            )
    with open(out, "w", newline="\n") as f:
        f.writelines(f"{read}\n" for read in sorted(ids))
    return len(ids)


def _bam_count(path: str, *options: str) -> int:
    out = run_command(["samtools", "view", "-c", *options, path], capture_output=True)
    return int(out.stdout.strip() or 0)


def _basecall_models(bam: str) -> dict[str, str]:
    """basecall_model and modbase_models from the BAM's @RG DS."""
    header = run_command(["samtools", "view", "-H", bam], capture_output=True).stdout
    found: dict[str, str] = {}
    for key in ("basecall_model", "modbase_models"):
        match = re.search(rf"\b{key}=(\S+)", header)
        if match:
            found[key] = match.group(1)
    return found


def dorado_command(
    dorado: str,
    model: str,
    source: Path,
    device: str,
    models_dir: Path,
    codes: list[str],
    read_ids: str | None,
) -> list[str]:
    command = [
        dorado, "basecaller", model, str(source),
        "-x", device, "--models-directory", str(models_dir),
    ]  # fmt: skip
    if source.is_dir():
        command.append("--recursive")
    if read_ids:
        command += ["--read-ids", read_ids]
    # Last: --modified-bases takes every value after it, and would swallow
    # the positional model and data.
    if codes:
        command += ["--modified-bases", *codes]
    return command


def _alignment_reference(params: BasecallParams, alignment: str) -> str | None:
    if not alignment.lower().endswith(".cram"):
        return None
    ref = resolve_reference(params.ref, None, alignment)
    if not ref:
        raise UsageError(f"--ref is needed to read the CRAM {alignment}.")
    return ref


def run_basecall(params: BasecallParams) -> None:
    source = Path(params.input).expanduser().absolute()
    codes = modification_codes(params.modified_bases)
    model_folder = check_model(params.model)
    model = str(Path(params.model).expanduser()) if model_folder else params.model
    pod5_files(source)
    device = resolve_device(params.device)
    verify_dependencies(["samtools"])
    dorado, version = resolve_dorado(params.dorado_path)
    models_dir = models_directory(params.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    subset = bool(params.read_ids or params.regions_bed)
    outdir = (
        Path(params.outdir).expanduser() if params.outdir else source.parent
    ).absolute()
    outdir.mkdir(parents=True, exist_ok=True)
    out_bam = str(outdir / output_name(source, model, codes, subset))

    result = current_result()
    if device == "cpu":
        warn(
            "basecall_on_cpu",
            "Basecalling on the CPU is very slow: about 850 bases a second with "
            "hac (sup is slower) and 35,000 with fast, so months or days for a "
            "MinION flow cell. Use an NVIDIA GPU or a Mac with Apple silicon for "
            "more than a few thousand reads.",
            device=device,
        )
    logging.info(
        f"Basecalling {source} with Dorado {version} ({model}"
        f"{', ' + ', '.join(codes) if codes else ''}) on {device}; models in "
        f"{models_dir}."
    )
    with tempfile.TemporaryDirectory(prefix=".wgse_basecall_", dir=outdir) as work:
        read_ids = str(params.read_ids) if params.read_ids else None
        if params.regions_bed and params.alignment:
            alignment = str(Path(params.alignment).expanduser())
            read_ids = str(Path(work, "read_ids.txt"))
            ref = _alignment_reference(params, alignment)
            wanted = panel_read_ids(alignment, str(params.regions_bed), ref, read_ids)
            result.add_input(alignment, "alignment")
            result.add_input(params.regions_bed, "regions_bed")
            if not wanted:
                raise UsageError(
                    f"{alignment} has no mapped reads in {params.regions_bed}."
                )
            logging.info(f"Re-basecalling the {wanted:,} reads in the regions.")
        command = dorado_command(
            dorado, model, source, device, models_dir, codes, read_ids
        )
        with (
            cleanup_files(
                on_failure=[out_bam], preserve_outputs=atomic_outputs_enabled()
            ),
            safe_output(out_bam) as staged,
            open(staged, "wb") as stream,
        ):
            try:
                run_command(command, stdout=stream)
            except (OSError, subprocess.SubprocessError) as error:
                raise WGSExtractError(f"Dorado failed: {error}") from error

    reads = _bam_count(out_bam)
    modified = _bam_count(out_bam, "--tag", "MM") if codes else 0
    models = _basecall_models(out_bam)
    result.add_output(out_bam, "unaligned_reads", kind="bam", metrics={"reads": reads})
    result.add_metrics(
        reads=reads,
        device=device,
        dorado_version=version,
        model=models.get("basecall_model", model),
    )
    if codes:
        result.add_metrics(
            modbase_models=models.get("modbase_models", ""),
            reads_with_modifications=modified,
        )
    if not reads:
        warn(
            "no_reads",
            "Dorado wrote no reads"
            + (
                ": none of the requested reads are in the POD5 input."
                if subset
                else "."
            ),
        )
    elif codes and not modified:
        warn(
            "no_modification_tags",
            f"No read got MM/ML tags for {', '.join(codes)}; methylation pileup "
            "will find nothing.",
            modifications=",".join(codes),
        )
    human_output(f"Unaligned BAM: {out_bam} ({reads:,} reads)")


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "basecall",
        parents=[base_parser],
        help=CLI_HELP["cmd_basecall"],
        description=CLI_HELP["cmd_basecall"],
    )
    add_arguments(parser, BasecallParams)
    parser.set_defaults(func=cli_handler(BasecallParams, run_basecall))


__all__ = ["models_directory", "register", "run_basecall"]
