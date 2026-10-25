"""The command-line parser: config defaults against explicit options, --genome,
the help and config commands."""

import sys
from argparse import Namespace
from pathlib import Path

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
        f'output_directory = "{tmp_path / "out"}"\n'
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
    _config(f'output_directory = "{outdir}"\n')

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

    main(["config"])

    assert path.exists() is created
    assert message in capsys.readouterr().out
    if created:
        assert path.read_text() == parser.DEFAULT_CONFIG


def test_config_bootstrap_survives_a_closed_stdin(monkeypatch, capsys, tmp_path):
    def closed(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)

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
def genome(tmp_path):
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
