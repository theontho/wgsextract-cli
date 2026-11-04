"""The command-line parser: config defaults against explicit options, --genome,
the help and config commands."""

import argparse
import sys
from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomllib

from wgsextract_cli.cli import parser
from wgsextract_cli.cli.dispatch import dispatch_command
from wgsextract_cli.core import config
from wgsextract_cli.core.config import KNOWN_SETTINGS, settings
from wgsextract_cli.main import main


def _config(text: str) -> Path:
    path = config.get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    config.reload_settings()
    return path


def _parse(argv: list[str]) -> tuple[Namespace, set[str]]:
    return parser.parse_args(*parser.build_parser(), argv)


@pytest.mark.parametrize("command", [["vep"], ["vep", "run"]])
@pytest.mark.parametrize("flag", ["--assembly", "--vep-assembly"])
def test_vep_run_assembly_aliases_preserve_the_runtime_destination(
    command, flag, caplog
):
    from wgsextract_cli.commands._vep_run import _apply_forced_assembly

    args, explicit = _parse([*command, flag, "GRCh37"])
    assert args.vep_assembly == "GRCh37"
    assert "vep_assembly" in explicit
    lib = SimpleNamespace(build="hg38", fasta="/lib/GRCh38.fa")
    _apply_forced_assembly(lib, args.vep_assembly)  # type: ignore[arg-type]
    assert lib.build == "hg19" and lib.fasta is None
    assert "--assembly GRCh37" in caplog.text
    assert "--vep-assembly" not in caplog.text


def test_vep_download_assembly_still_selects_the_cache_build():
    args, _ = _parse(["vep", "download", "--assembly", "GRCh37"])
    assert args.assembly == "GRCh37"


def test_config_supplies_defaults_that_options_override():
    _config(
        'input = "/cfg/in.bam"\noutdir = "/cfg/out"\nref = "/cfg/ref.fa"\n'
        'memory = "5G"\nthreads = 6\ndebug_mode = true\n'
    )

    args, explicit = _parse(["info"])
    assert (args.input, args.outdir, args.ref) == (
        "/cfg/in.bam",
        "/cfg/out",
        "/cfg/ref.fa",
    )
    assert (args.memory, args.threads, args.debug) == ("5G", 6, True)
    assert explicit == set() and args._explicit_dests == set()

    args, explicit = _parse(["-i", "x.bam", "info", "--memory=3G", "-t", "2"])
    assert (args.input, args.memory, args.threads) == ("x.bam", "3G", 2)
    assert args.outdir == "/cfg/out"
    assert explicit == {"input", "memory", "threads"} == args._explicit_dests


def test_shared_flags_and_typed_options_are_explicit():
    args, explicit = _parse(["--parent-pid", "42", "info", "--quiet", "--debug"])

    assert args.parent_pid == 42
    assert args.quiet is True and args.debug is True
    assert explicit == {"parent_pid", "quiet", "debug"}


def test_parse_args_reads_sys_argv_by_default(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wgsextract", "info", "--outdir", "o"])

    args, explicit = parser.parse_args(*parser.build_parser())

    assert (args.command, args.outdir, explicit) == ("info", "o", {"outdir"})


def test_help_command_prints_the_command_tree(capsys):
    main(["help"])

    out = capsys.readouterr().out
    assert "GLOBAL OPTIONS:" in out and "--genome" in out
    tree = out.split("COMMAND TREE:", 1)[1].splitlines()
    deps = next(i for i, line in enumerate(tree) if line.startswith("- deps"))
    assert any(line.startswith("  - check") for line in tree[deps + 1 :])


def test_full_help_flag_prints_the_tree_and_exits(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--full-help"])

    assert exit_info.value.code == 0
    assert "COMMAND TREE:" in capsys.readouterr().out


def test_no_command_prints_usage(capsys):
    main([])

    assert "usage:" in capsys.readouterr().out


def test_config_command_shows_set_values_and_redacts_keys(capsys, tmp_path):
    path = _config(
        f"output_directory = '{tmp_path / 'out'}'\n"
        'signing_key = "/home/me/.ssh/id_ed25519"\n'
    )

    main(["config"])

    out = capsys.readouterr().out
    assert f"Config File:      {path}" in out
    lines = {line.split()[0]: line for line in out.splitlines() if line.strip()}
    assert str(tmp_path / "out") in lines["output_directory"]
    assert "[SET]" in lines["output_directory"]
    assert "<redacted>" in lines["signing_key"] and "id_ed25519" not in out
    assert "[DEFAULT/UNSET]" in lines["reference_library"]
    assert "does not exist yet" not in out


def test_showing_the_config_does_not_create_the_output_directory(tmp_path):
    outdir = tmp_path / "media" / "nas" / "wgse"
    _config(f"output_directory = '{outdir}'\n")

    main(["config"])

    assert not (tmp_path / "media").exists()


@pytest.mark.parametrize(
    ("answer", "created", "message"),
    [("y", True, "Created default config"), ("n", False, "Skipping bootstrap")],
)
def test_config_command_offers_to_bootstrap_a_default_config(
    monkeypatch, capsys, answer, created, message
):
    path = config.get_config_path()
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    main(["config"])

    assert path.exists() is created
    assert message in capsys.readouterr().out
    if created:
        assert path.read_text() == parser.DEFAULT_CONFIG


def test_config_bootstrap_survives_a_closed_stdin(monkeypatch, capsys, tmp_path):
    def closed(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    parser.bootstrap_default_config(tmp_path / "cfg" / "config.toml")

    assert "Bootstrap cancelled." in capsys.readouterr().out
    assert not (tmp_path / "cfg").exists()


def test_every_default_config_entry_is_a_setting_once_uncommented():
    """The bootstrapped file is all comments; uncommenting an example line must
    set a real setting, not a key the loader silently ignores."""
    assert tomllib.loads(parser.DEFAULT_CONFIG) == {}
    examples = [
        line.lstrip("# ").split("#")[0]
        for line in parser.DEFAULT_CONFIG.splitlines()
        if " = " in line
    ]
    uncommented = tomllib.loads("\n".join(examples))
    for key in uncommented:
        assert config.CONFIG_ALIASES.get(key, key) in KNOWN_SETTINGS, key


# --genome replaces the inputs the user didn't give with the genome folder's.


@pytest.fixture
def genome(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "wgsextract_cli.core.alignment_input.validate_alignment_input",
        lambda path: None,
    )
    library = tmp_path / "genomes"
    folder = library / "ken"
    (folder / "calls").mkdir(parents=True)
    for name in ("ken.bam", "calls/ken.vcf.gz", "ken_R1.fastq.gz", "ken_R2.fastq.gz"):
        (folder / name).write_text("x")
    settings["genome_library"] = str(library)
    return folder


def _dispatched(argv: list[str]) -> Namespace:
    """The arguments a command function receives for this command line."""
    args, explicit = _parse(argv)
    seen = []
    args.func = seen.append
    dispatch_command(args, explicit)
    return seen[0]


def test_genome_fills_input_and_outdir_but_explicit_ones_win(genome, tmp_path):
    args = _dispatched(["--genome", "ken", "info"])
    assert (args.input, args.outdir) == (str(genome / "ken.bam"), str(genome))

    out = tmp_path / "mine"
    args = _dispatched(
        ["info", "--genome", "ken", "-i", "my.bam", "--outdir", str(out)]
    )
    assert (args.input, args.outdir) == ("my.bam", str(out))
    assert out.is_dir()


@pytest.mark.parametrize(
    "argv",
    [
        ["--genome", "ken", "info", "-imy.bam"],
        ["--genome", "ken", "info", "--inp", "my.bam"],
    ],
)
def test_genome_keeps_an_input_in_any_spelling_argparse_accepts(genome, argv):
    assert _dispatched(argv).input == "my.bam"


@pytest.mark.parametrize(
    ("argv", "field", "expected"),
    [
        (
            ["vcf", "filter", "--vcf-input", "my.vcf.gz", "--expr", "QUAL>1"],
            "vcf_input",
            "my.vcf.gz",
        ),
        (
            [
                "vcf",
                "trio",
                "--proband",
                "p.vcf.gz",
                "--mother",
                "m.vcf.gz",
                "--father",
                "f.vcf.gz",
            ],
            "proband",
            "p.vcf.gz",
        ),
        (["align", "--r1", "a_R1.fq.gz", "--r2", "a_R2.fq.gz"], "r1", "a_R1.fq.gz"),
    ],
)
def test_genome_keeps_command_inputs_the_user_gave(genome, argv, field, expected):
    args = _dispatched(["--genome", "ken", *argv])
    assert getattr(args, field) == expected


def test_tokens_after_double_dash_are_not_options():
    args, explicit = _parse(["example-genome", "download", "--", "--debug"])

    assert args.example_ids == ["--debug"]
    assert args.debug is False and "debug" not in explicit


# Global options parse the same before the command and after it (#348).

GLOBALS = [
    "--input", "in.bam", "-o", "out", "--genome", "g", "--ref", "r.fa",
    "-t", "2", "--memory=3G", "--tmp-dir", "tmp", "--json", "--quiet",
    "--debug", "--log-file", "run.log", "--keep-awake", "--no-atomic-output",
    "--config", "c.toml", "--parent-pid", "7",
]  # fmt: skip


def _commands(
    command: argparse.ArgumentParser, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    """Every runnable command, once per parser (aliases skipped)."""
    seen: list[argparse.ArgumentParser] = []
    groups = [a for a in command._actions if isinstance(a, argparse._SubParsersAction)]
    if path and not any(group.required for group in groups):
        yield path, command
    for group in groups:
        for name, sub in group.choices.items():
            if not any(sub is done for done in seen):
                seen.append(sub)
                yield from _commands(sub, (*path, name))


def _required_args(command: argparse.ArgumentParser) -> list[str]:
    """Placeholder values for the command's own required arguments."""
    argv: list[str] = []
    groups = [g for g in command._mutually_exclusive_groups if g.required]
    for action in command._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        if action.nargs == 0:
            argv += action.option_strings[:1] if action.required else []
            continue
        value = str(action.choices and next(iter(action.choices)) or "1")
        if not action.option_strings:
            if action.nargs not in ("?", "*"):
                argv.append(value)
        elif action.required or any(g._group_actions[0] is action for g in groups):
            argv += [action.option_strings[0], value]
    return argv


# The tree's shape doesn't depend on the config, so it can be read at collection.
COMMAND_PATHS = [path for path, _ in _commands(parser.build_parser()[0])]


@pytest.mark.parametrize("path", COMMAND_PATHS, ids=" ".join)
def test_global_options_parse_the_same_before_or_after_the_command(path):
    built = parser.build_parser()
    required = _required_args(dict(_commands(built[0]))[path])
    before, explicit = parser.parse_args(*built, [*GLOBALS, *path, *required])
    assert before.input == "in.bam" and before.parent_pid == 7
    for split in range(1, len(path) + 1):
        argv = [*path[:split], *GLOBALS, *path[split:], *required]
        after, after_explicit = parser.parse_args(*built, argv)
        assert vars(after) == vars(before), argv
        assert after_explicit == explicit


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["-i", "a.bam", "info", "--input", "b.bam"], "'-i a.bam' and '--input b.bam'"),
        (["vcf", "-t", "auto", "snp", "-t", "4"], "'-t auto' and '-t 4'"),
        (["--keep-awake", "info", "--no-keep-awake"], "'--keep-awake' and '--no-keep-awake'"),
        (["--outdir", "a", "--outdir", "b", "info", "-o", "a"], "'--outdir b' and '-o a'"),
        # Only each level's last value counts, whatever the order (#371 review).
        (["--input", "a", "info", "--input", "a", "--input", "b"], "'--input a' and '--input b'"),
        (["--keep-awake", "info", "--keep-awake", "--no-keep-awake"], "'--keep-awake' and '--no-keep-awake'"),
        (["--no-atomic-output", "vcf", "--atomic-output", "snp", "--no-atomic-output"], "'--no-atomic-output' and '--atomic-output'"),
    ],
)  # fmt: skip
def test_conflicting_global_options_are_a_usage_error(argv, message, capsys):
    with pytest.raises(SystemExit) as exit_info:
        _parse(argv)

    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_a_global_option_repeated_with_the_same_value_is_allowed():
    args, explicit = _parse(["-i", "a.bam", "--json", "info", "--input", "a.bam", "--json"])  # fmt: skip
    assert (args.input, args.json, explicit) == ("a.bam", True, {"input", "json"})


@pytest.mark.parametrize(
    ("argv", "dest", "expected"),
    [
        (["-i", "a", "info", "-i", "b", "-i", "a"], "input", "a"),
        (["--no-keep-awake", "info", "--keep-awake", "--no-keep-awake"], "keep_awake", False),
    ],
)  # fmt: skip
def test_levels_agreeing_on_their_last_value_are_not_a_conflict(argv, dest, expected):
    args, _ = _parse(argv)
    assert getattr(args, dest) == expected


def test_the_last_global_option_wins_within_one_command_level():
    args, _ = _parse(["info", "-t", "2", "--input", "a.bam", "-t", "1", "-i", "b.bam"])
    assert (args.threads, args.input) == (1, "b.bam")


def test_config_defaults_do_not_hide_a_global_given_before_the_command():
    _config('input = "/cfg/in.bam"\nthreads = 6\n')

    args, _ = _parse(["--input", "x.bam", "vcf", "snp", "-t", "3"])

    assert (args.input, args.threads) == ("x.bam", 3)


def test_command_help_lists_global_options_in_one_short_group(capsys):
    with pytest.raises(SystemExit):
        main(["vcf", "snp", "--help"])

    out = capsys.readouterr().out
    usage, rest = out.split("\n\n", 1)
    assert "--input" not in usage and "--json" not in usage
    globals_help = rest.split("global options:", 1)[1]
    assert "-i/--input" in globals_help and "wgsextract --help" in globals_help
    options = out.split("\nexamples:", 1)[0]
    assert options.count("--input") == 1


def test_a_command_option_clashing_with_a_global_option_is_rejected(monkeypatch):
    class Clashing:
        @staticmethod
        def register(subparsers, base_parser):
            subparsers.add_parser("clash").add_argument("--threads")

    monkeypatch.setattr(parser, "COMMAND_MODULES", [Clashing])

    with pytest.raises(RuntimeError, match="'wgsextract clash'.*--threads"):
        parser.build_parser()
