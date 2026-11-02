"""Clair3 small-variant calling for Oxford Nanopore reads (optional caller)."""

import argparse
import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.atomic_output import safe_output
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.output_checks import warn_if_no_variants
from wgsextract_cli.core.paths import expand_path
from wgsextract_cli.core.pixi_tools import pixi_env_prefix
from wgsextract_cli.core.reference_assets import read_fai_lengths
from wgsextract_cli.core.ultima import read_groups
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import ensure_vcf_indexed

from .basic import get_base_args, plain_reference
from .callers import write_region_bed

CLAIR3 = "run_clair3.sh"
MODEL_FILES = ("pileup.pt", "full_alignment.pt")
WINDOWS_UNSUPPORTED = (
    "Clair3 is not available on Windows (Bioconda builds it for Linux only). "
    "Run `vcf clair3` on Linux x86-64, for example under WSL2."
)
_FETCH = (
    "Download a model directory (pileup.pt and full_alignment.pt) from "
    "https://www.bio8.cs.hku.hk/clair3/clair3_models_rerio_pytorch/ and pass "
    "it with --model or clair3_model in config.toml; see docs/ont.md."
)


@dataclass(frozen=True)
class Chemistry:
    """Pore/kit/speed must match the model; basecaller accuracy/version should."""

    pore: str
    kit: str | None = None
    speed: str | None = None
    accuracy: str | None = None
    version: str | None = None

    def key(self) -> tuple[str, str | None, str | None]:
        # R9.4.1 models are trained across kits and speeds.
        if self.pore == "941":
            return (self.pore, None, None)
        return (self.pore, self.kit, self.speed)


# Dorado (dna_r10.4.1_e8.2_400bps_sup@v4.1.0) and Guppy (dna_r9.4.1_450bps_hac)
# basecaller model names, as recorded in @RG DS / ID and @PG.
_BASECALLER = re.compile(
    r"dna_r(?P<pore>\d+(?:\.\d+)+)(?:_e(?P<kit>\d+(?:\.\d+)*))?(?:_\d+khz)?"
    r"(?:_(?P<speed>\d+)bps)?(?:_\d+khz)?_(?P<accuracy>fast|hac|sup)"
    r"(?:@v(?P<version>\d+(?:\.\d+)*))?",
    re.IGNORECASE,
)
_MODEL = re.compile(
    r"^r(?P<pore>\d+)(?:_e(?P<kit>\d+))?(?:_(?P<speed>\d+)bps)?(?:_prom)?"
    r"_(?P<accuracy>fast|hac|sup)(?:_(?P<version>[vg][0-9g+]+))?$"
)


def basecaller_chemistries(header: str) -> set[Chemistry]:
    """Basecaller models named in the @RG and @PG header lines."""
    found = set()
    for line in header.splitlines():
        if not line.startswith(("@RG\t", "@PG\t")):
            continue
        for match in _BASECALLER.finditer(line):
            version = match["version"]
            found.add(
                Chemistry(
                    pore=match["pore"].replace(".", ""),
                    kit=match["kit"].replace(".", "") if match["kit"] else None,
                    speed=match["speed"],
                    accuracy=match["accuracy"].lower(),
                    version=f"v{version.replace('.', '')}" if version else None,
                )
            )
    return found


def model_chemistry(name: str) -> Chemistry | None:
    """What a Clair3 ONT model name says it was trained on."""
    if name in ("ont", "ont_guppy5"):
        return Chemistry(pore="941")
    match = _MODEL.match(name)
    if not match:
        return None
    return Chemistry(
        pore=match["pore"],
        kit=match["kit"],
        speed=match["speed"],
        accuracy=match["accuracy"],
        version=match["version"],
    )


def model_for_chemistry(chemistry: Chemistry) -> str | None:
    """The Clair3 model trained for a basecaller, or None when unknown."""
    if chemistry.pore == "941":
        # HKU: also applicable to HAC and fast R9.4.1 reads.
        return "r941_prom_sup_g5014"
    if chemistry.pore == "104" and chemistry.kit == "81" and chemistry.accuracy:
        return f"r104_e81_{chemistry.accuracy}_g5015"
    if (
        chemistry.pore == "1041"
        and chemistry.kit == "82"
        and chemistry.accuracy
        and chemistry.version
    ):
        speed = chemistry.speed or "400"
        return f"r1041_e82_{speed}bps_{chemistry.accuracy}_{chemistry.version}"
    return None


def _chemistry_text(chemistry: Chemistry) -> str:
    pore = {"1041": "R10.4.1", "104": "R10.4", "941": "R9.4.1"}.get(
        chemistry.pore, f"r{chemistry.pore}"
    )
    parts = [pore]
    if chemistry.kit and chemistry.pore != "941":
        parts.append(f"E{chemistry.kit[0]}.{chemistry.kit[1:]}")
    if chemistry.speed and chemistry.pore != "941":
        parts.append(f"{chemistry.speed} bps")
    return " ".join(parts)


@lru_cache(maxsize=1)
def pinned_models() -> dict[str, dict[str, object]]:
    manifest = Path(__file__).resolve().parents[2] / "assets/clair3/models.json"
    models: dict[str, dict[str, object]] = json.loads(manifest.read_text("utf-8"))[
        "models"
    ]
    return models


def require_supported_platform() -> None:
    if sys.platform == "win32":
        raise WGSExtractError(WINDOWS_UNSUPPORTED)
    if not sys.platform.startswith("linux") or platform.machine().lower() not in {
        "x86_64",
        "amd64",
    }:
        raise WGSExtractError(
            "Clair3 is qualified on Linux x86-64 only; Bioconda's builds for other "
            "platforms are not validated here. Use `vcf deepvariant` (the ONT "
            "default) on this host."
        )


def bundled_models_dir() -> Path | None:
    """Bioconda ships its pre-trained models in the package's bin/models."""
    tool = get_tool_path(CLAIR3)
    candidates = []
    if tool and Path(tool).is_file():
        candidates += [
            Path(tool).parent / "models",
            Path(tool).resolve().parent / "models",
        ]
    elif prefix := pixi_env_prefix("clair3"):
        candidates.append(prefix / "bin" / "models")
    return next((path for path in candidates if path.is_dir()), None)


def _fetch_hint(name: str) -> str:
    pin = pinned_models().get(name)
    if not pin:
        return _FETCH
    return (
        f"Download {name} ({', '.join(MODEL_FILES)}) from {pin['url']} into a "
        f"directory named {name} and pass it with --model or clair3_model in "
        "config.toml; see docs/ont.md."
    )


def resolve_model(value: str, bundled: Path | None) -> Path:
    """A model directory, or the name of one Clair3 bundles."""
    path = Path(expand_path(value))
    if path.is_dir():
        model = path
    elif bundled and "/" not in value and (bundled / value).is_dir():
        model = bundled / value
    else:
        raise WGSExtractError(
            f"Clair3 model {value} isn't installed. {_fetch_hint(path.name)}"
        )
    missing = [name for name in MODEL_FILES if not (model / name).is_file()]
    if missing:
        raise WGSExtractError(
            f"Clair3 model {model} lacks {', '.join(missing)}; Clair3 2.x needs "
            f"PyTorch models. {_fetch_hint(model.name)}"
        )
    return model


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(model: Path) -> None:
    """Check a known model against its pinned SHA-256 digests."""
    pin = pinned_models().get(model.name)
    if pin is None:
        logging.warning(
            f"Clair3 model {model.name} has no pinned checksum; its integrity and "
            "chemistry are unverified."
        )
        return
    expected = pin["files"]
    assert isinstance(expected, dict)
    for name, digest in expected.items():
        if _sha256(model / name) != digest:
            raise WGSExtractError(
                f"Clair3 model file {model / name} doesn't match its pinned "
                f"SHA-256; the download is corrupt or not {model.name}. "
                f"{_fetch_hint(model.name)}"
            )
    logging.info(f"Verified Clair3 model {model.name} (SHA-256).")


def check_model_matches(model_name: str, detected: set[Chemistry]) -> None:
    """Refuse a model for another chemistry; warn on another basecaller."""
    if model_name.startswith(("hifi", "ilmn")):
        raise WGSExtractError(
            f"Clair3 model {model_name} is not an ONT model; `vcf clair3` calls "
            "Nanopore reads."
        )
    if "with_mv" in model_name:
        raise WGSExtractError(
            f"Clair3 model {model_name} needs Dorado move tables (mv tags) and "
            "--enable_dwell_time, which this command does not use; choose the "
            "matching model without _with_mv."
        )
    trained = model_chemistry(model_name)
    if trained is None or not detected:
        return
    for chemistry in detected:
        model_key = trained.key()
        reads_key = chemistry.key()
        if any(
            mine is not None and theirs is not None and mine != theirs
            for mine, theirs in zip(model_key, reads_key, strict=True)
        ):
            raise WGSExtractError(
                f"Clair3 model {model_name} is for {_chemistry_text(trained)} reads, "
                f"but the basecaller metadata says {_chemistry_text(chemistry)}. "
                f"Use {model_for_chemistry(chemistry) or 'a model for that chemistry'}."
            )
        if trained.pore == "941":
            continue
        for field in ("accuracy", "version"):
            mine, theirs = getattr(trained, field), getattr(chemistry, field)
            if mine and theirs and mine != theirs:
                logging.warning(
                    f"Clair3 model {model_name} was trained for a different basecaller "
                    f"{field} ({mine}) than these reads ({theirs}); accuracy may "
                    f"suffer. Expected model: {model_for_chemistry(chemistry)}."
                )
                break


def select_model(args: argparse.Namespace, header: str) -> Path:
    """--model, then clair3_model in config.toml, then the basecaller metadata."""
    detected = basecaller_chemistries(header)
    bundled = bundled_models_dir()
    value = getattr(args, "model", None) or settings.get("clair3_model")
    if not value:
        names = {model_for_chemistry(chemistry) for chemistry in detected}
        if len(names) != 1 or None in names:
            reason = (
                "the reads come from several basecaller models"
                if len(detected) > 1
                else "the BAM/CRAM header doesn't name a known basecaller model"
            )
            available = ", ".join(
                sorted(
                    p.name
                    for p in (bundled.iterdir() if bundled else ())
                    if p.is_dir() and model_chemistry(p.name)
                )
            )
            raise WGSExtractError(
                f"Can't choose a Clair3 model: {reason}. Pass --model NAME|DIR "
                f"(or set clair3_model in config.toml) for the basecaller that made "
                f"these reads. Bundled ONT models: {available or 'none found'}."
            )
        value = str(names.pop())
        logging.info(f"Basecaller metadata selects Clair3 model {value}.")
    model = resolve_model(str(value), bundled)
    check_model_matches(model.name, detected)
    verify_model(model)
    return model


def sample_name(header: str, reads: str) -> str:
    """The reads' @RG SM (Clair3 would otherwise write "SAMPLE")."""
    samples = {group["SM"] for group in read_groups(header) if group.get("SM")}
    if len(samples) > 1:
        raise WGSExtractError(
            f"The read groups name several samples ({', '.join(sorted(samples))}); "
            "Clair3 calls one sample per run. Split the BAM/CRAM by sample first."
        )
    if samples:
        return samples.pop()
    name = Path(reads).name.split(".")[0]
    logging.warning(f"No @RG SM sample name in {reads}; naming the sample {name}.")
    return name


def require_ont_read_groups(header: str) -> None:
    """Reads from other platforms must not enter an ONT model."""
    platforms = {
        group["PL"].upper() for group in read_groups(header) if group.get("PL")
    }
    if platforms - {"ONT"}:
        raise WGSExtractError(
            f"The read groups name platform(s) {', '.join(sorted(platforms))}; "
            "`vcf clair3` calls Nanopore (PL:ONT) reads only. Use `vcf deepvariant` "
            "for other platforms, or split out the ONT read groups."
        )


def clair3_command(
    reads: str,
    ref: str,
    model: Path,
    threads: str,
    output: str,
    bed: str | None,
    sample: str,
) -> list[str]:
    command = [
        CLAIR3,
        f"--bam_fn={reads}",
        f"--ref_fn={ref}",
        f"--threads={threads}",
        "--platform=ont",
        f"--model_path={model}",
        f"--sample_name={sample}",
        f"--output={output}",
    ]
    if bed:
        command.append(f"--bed_fn={bed}")
    return command


def _clair3_env() -> dict[str, str]:
    # Clair3 runs its own Python; don't leak ours into it.
    env = dict(os.environ)
    for var in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(var, None)
    return env


def cmd_clair3(args: argparse.Namespace) -> None:
    require_supported_platform()
    verify_dependencies([CLAIR3, "samtools", "bcftools", "tabix"])
    log_dependency_info([CLAIR3])
    threads, outdir, ref, _lib = get_base_args(args)
    detected = getattr(args, "input_platform", None)
    if detected not in (None, "ont"):
        raise WGSExtractError(
            f"The input looks like {detected} reads; `vcf clair3` calls Nanopore "
            "reads. Use `vcf deepvariant` for other platforms."
        )
    header = get_bam_header(args.input, ref)
    require_ont_read_groups(header)
    sample = sample_name(header, args.input)
    model = select_model(args, header)

    out_vcf = str(Path(outdir, "clair3.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_calling_clair3"].format(output=out_vcf))
    with (
        plain_reference(ref, outdir) as use_ref,
        tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_clair3_") as work,
    ):
        bed = None
        if args.region:
            bed = str(Path(work, "regions.bed"))
            write_region_bed(args.region, read_fai_lengths(use_ref), bed)
        output = str(Path(work, "clair3"))
        try:
            # stdin closed: Clair3's GNU parallel would read it otherwise.
            run_command(
                clair3_command(
                    args.input, use_ref, model, threads, output, bed, sample
                ),
                env=_clair3_env(),
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Clair3 failed: {e}") from e
        merged = Path(output, "merge_output.vcf.gz")
        if not merged.is_file():
            raise WGSExtractError("Clair3 failed to produce merge_output.vcf.gz.")
        with safe_output(out_vcf) as staged:
            run_command(["bcftools", "view", "-Oz", "-o", staged, str(merged)])
    ensure_vcf_indexed(out_vcf)
    warn_if_no_variants(out_vcf, ref, args.region)
