"""Fast header/EOF checks before reference inference or expensive processing."""

import gzip
import zlib
from pathlib import Path

from wgsextract_cli.core.paths import PathInput
from wgsextract_cli.core.utils import WGSExtractError, run_command


def validate_alignment_input(path: PathInput) -> None:
    file = Path(path)
    if not file.is_file():
        raise WGSExtractError(f"Input file not found: {file}")
    try:
        with file.open("rb") as stream:
            magic = stream.read(4)
        if file.suffix.lower() == ".cram":
            valid_magic = magic == b"CRAM"
        elif file.suffix.lower() == ".bam":
            with gzip.open(file, "rb") as stream:
                valid_magic = stream.read(4) == b"BAM\x01"
        else:
            raise WGSExtractError(f"Expected a BAM or CRAM alignment input: {file}")
    except (OSError, EOFError, zlib.error) as error:
        raise WGSExtractError(f"Invalid alignment input {file}: {error}") from error
    if not valid_magic:
        raise WGSExtractError(
            f"Invalid alignment input {file}: file format does not match its extension"
        )
    # -u permits legitimate unaligned BAMs without sequence dictionary entries.
    result = run_command(
        ["samtools", "quickcheck", "-v", "-u", file], capture_output=True, check=False
    )
    if result.returncode:
        detail = (
            result.stderr or result.stdout or "missing header or EOF block"
        ).strip()
        raise WGSExtractError(
            f"Invalid alignment input {file}: samtools quickcheck failed: {detail[-2000:]}"
        )
