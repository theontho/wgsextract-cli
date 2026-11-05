import argparse
import contextlib
import logging
import sys
from pathlib import Path

from wgsextract_cli.commands._command_needs import COMMAND_NEEDS, HOURS, MINUTES
from wgsextract_cli.core import live_status
from wgsextract_cli.core.genome_library import apply_genome_selection
from wgsextract_cli.core.keep_awake import keep_awake
from wgsextract_cli.core.last_run import LogTail, record_last_run
from wgsextract_cli.core.results import (
    CommandResult,
    collecting,
    fail_from_exception,
    params_from_args,
    redact_argv,
    stdout_reserved,
)
from wgsextract_cli.core.run_log import active_log_path
from wgsextract_cli.core.sample_workspace import SampleWorkspace, active_workspace

from .hints import print_next_steps
from .platform_support import verify_platform_support

# Commands that only report: they mustn't create the configured output folder
# (it may be on a drive that isn't mounted), check inputs or need a temp dir.
# `sample` only reads and edits the small workspace file.
READ_ONLY_COMMANDS = frozenset({"help", "config", "deps", "report-issue", "sample"})
_NO_ALIGNMENT_PREFLIGHT = {*READ_ONLY_COMMANDS, "ref", "benchmark", "sign"}


def checks_alignment_input(path: str) -> bool:
    """Whether the command at ``path`` (e.g. 'vcf snp') runs ``samtools
    quickcheck`` on a BAM/CRAM ``--input`` before its handler starts."""
    words = path.split()
    exempt = not words or words[0] in _NO_ALIGNMENT_PREFLIGHT
    return not exempt and words != ["qc", "fake-data"]


# Arguments that name a command's primary input files, and their manifest role.
INPUT_ARGS = {
    "input": "input",
    "vcf_input": "vcf_input",
    "r1": "fastq_r1",
    "r2": "fastq_r2",
}


def dispatch_command(args: argparse.Namespace, explicit_dests: set[str]) -> None:
    """Run the parsed command and exit with its status code.

    Exit codes: 0 ok, 1 failure, 2 usage error, 3 missing dependency (and
    128+N when stopped by signal N). The command's result manifest is written
    next to its outputs; with --json it is also the only thing on stdout.
    On a terminal, a long run shows a progress line on stderr while it works
    and a successful run ends with 'Next:' hints there.
    """
    if not hasattr(args, "func"):
        raise RuntimeError("No command function is registered for parsed arguments.")

    as_json = bool(getattr(args, "json", False))
    result = CommandResult(
        command=getattr(args, "_command", None)
        or str(getattr(args, "command", None) or "wgsextract")
    )
    result.provenance.argv = redact_argv(getattr(args, "_argv", None) or sys.argv[1:])
    result.provenance.params = params_from_args(args)
    reserved = stdout_reserved() if as_json else contextlib.nullcontext(sys.stdout)
    tail = LogTail()
    root = logging.getLogger()
    root.addHandler(tail)
    with reserved as stdout, collecting(result, as_json=as_json):
        try:
            with _progress_line(args, result.command):
                _run(args, explicit_dests, result)
        finally:
            root.removeHandler(tail)
        has_manifest = result.manifest_path() is not None
        result.finalize(probe_tools=as_json or has_manifest)
        try:
            if has_manifest and (manifest := result.write_manifest()):
                _index_output(args, manifest, result)
        except OSError as e:
            logging.warning(f"Could not write the result manifest: {e}")
        record_last_run(result, log_tail=list(tail.lines), log_file=active_log_path())
        if result_file := getattr(args, "_result_file", None):
            try:
                Path(result_file).write_text(
                    result.to_json(), encoding="utf-8", newline="\n"
                )
            except OSError as e:
                logging.warning(f"Could not write the result to {result_file}: {e}")
        if as_json:
            stdout.write(result.to_json())
            stdout.flush()
    if result.exit_code:
        sys.exit(result.exit_code)
    print_next_steps(args, result)


def _progress_line(
    args: argparse.Namespace, command: str
) -> contextlib.AbstractContextManager[object]:
    """The terminal's progress line, for a command that takes minutes or more."""
    needs = COMMAND_NEEDS.get(command)
    if needs is None or needs.runtime not in (MINUTES, HOURS):
        return contextlib.nullcontext()
    stream = sys.stderr
    as_json = bool(getattr(args, "json", False))
    quiet = bool(getattr(args, "quiet", False))
    if not live_status.wanted(stream, json=as_json, quiet=quiet):
        return contextlib.nullcontext()
    return live_status.LiveStatus(command, stream)


def _index_output(
    args: argparse.Namespace, manifest: Path, result: CommandResult
) -> None:
    """List a --genome run's manifest in the genome's sample workspace."""
    genome_dir = getattr(args, "genome_dir", None)
    if genome_dir and manifest.is_relative_to(Path(genome_dir).absolute()):
        SampleWorkspace(genome_dir).record_output(
            manifest, result.command, result.status
        )


def _run(
    args: argparse.Namespace, explicit_dests: set[str], result: CommandResult
) -> None:
    try:
        verify_platform_support(args)
        apply_genome_selection(args, set(explicit_dests))
        with active_workspace(
            getattr(args, "genome_dir", None),
            (getattr(args, dest, None) for dest in INPUT_ARGS),
        ):
            _run_command(args, explicit_dests, result)
    except (Exception, KeyboardInterrupt, SystemExit) as e:
        fail_from_exception(result, e, debug=bool(getattr(args, "debug", False)))


def _run_command(
    args: argparse.Namespace, explicit_dests: set[str], result: CommandResult
) -> None:
    # --genome fills in input/outdir; preflight failures keep the snapshot.
    result.provenance.params = params_from_args(args)
    for dest, role in INPUT_ARGS.items():
        value = getattr(args, dest, None)
        if isinstance(value, str) and value and Path(value).expanduser().exists():
            result.add_input(value, role)
    input_path = getattr(args, "input", None)
    command = getattr(args, "command", None) or ""
    if (
        input_path
        and "input" in explicit_dests
        and checks_alignment_input(f"{command} {getattr(args, 'qc_cmd', '') or ''}")
        and Path(input_path).suffix.lower() in {".bam", ".cram"}
    ):
        from wgsextract_cli.core.alignment_input import validate_alignment_input

        validate_alignment_input(input_path)
    if args.outdir and getattr(args, "command", None) not in READ_ONLY_COMMANDS:
        Path(args.outdir).mkdir(parents=True, exist_ok=True)
    from wgsextract_cli.core.atomic_output import atomic_output_mode

    with keep_awake(
        bool(getattr(args, "keep_awake", False))
        and getattr(args, "command", None) not in READ_ONLY_COMMANDS
    ):
        from wgsextract_cli.core.temp_directory import run_temporary_directory

        with atomic_output_mode(bool(getattr(args, "atomic_output", False))):
            if getattr(args, "command", None) in READ_ONLY_COMMANDS:
                args.func(args)
            else:
                with run_temporary_directory(getattr(args, "tmp_dir", None)):
                    args.func(args)
