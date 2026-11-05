"""Sample workspaces: a fact is detected once and then read back, a changed
file is detected again, the user's override wins downstream, and a corrupt,
older or newer wgsextract.sample.toml degrades gracefully."""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest

from tests.htslib_helpers import needs_htslib, sam_to_bam, write_fasta
from wgsextract_cli import api
from wgsextract_cli.commands.info import metrics
from wgsextract_cli.core import genotype_qc, sample_workspace
from wgsextract_cli.core.results import CommandResult, collecting
from wgsextract_cli.core.sample_ploidy import sex_samples_args
from wgsextract_cli.core.sample_workspace import (
    WORKSPACE_FILE,
    SampleWorkspace,
    active_workspace,
)
from wgsextract_cli.core.sequencing import input_platform
from wgsextract_cli.main import main

LENGTHS = {"chr1": 1000, "chr7": 1000, "chrX": 1000, "chrY": 1000}
MALE = {"chr1": 20, "chr7": 20, "chrX": 10, "chrY": 4}
FEMALE = {"chr1": 20, "chr7": 20, "chrX": 20}
QUAL = "I" * 50


@pytest.fixture(scope="module")
def seqs(tmp_path_factory):
    import random

    rng = random.Random(7)
    seqs = {
        c: "".join(rng.choice("ACGT") for _ in range(n)) for c, n in LENGTHS.items()
    }
    write_fasta(tmp_path_factory.mktemp("ref") / "ref.fa", seqs)
    return seqs


def _bam(seqs, path: Path, counts: dict[str, int]) -> str:
    header = [f"@SQ\tSN:{c}\tLN:{n}" for c, n in LENGTHS.items()]
    reads = []
    for contig, n in counts.items():
        for i in range(n):
            pos = 1 + (i * 13) % 900
            name = f"A00123:8:HABCDEFXX:2:1101:{list(LENGTHS).index(contig)}:{i}"
            seq = seqs[contig][pos - 1 : pos + 49]
            reads.append(f"{name}\t0\t{contig}\t{pos}\t60\t50M\t*\t0\t0\t{seq}\t{QUAL}")
    return sam_to_bam(header + reads, path)


@pytest.fixture
def folder(tmp_path):
    """A genome folder, as the genome library creates."""
    (tmp_path / "genome-config.toml").write_text("")
    return tmp_path


def _cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = 0
    with redirect_stdout(out), patch.object(sys, "argv", ["wgsextract", *argv]):
        try:
            main()
        except SystemExit as e:
            code = int(e.code or 0)
    return code, out.getvalue()


def _idxstats_counter():
    return patch.object(metrics, "parse_idxstats", wraps=metrics.parse_idxstats)


@needs_htslib
def test_sex_is_detected_once_then_read_back(seqs, folder):
    bam = _bam(seqs, folder / "s.bam", MALE)
    with _idxstats_counter() as idxstats:
        assert metrics.sample_sex(bam) == "Male"
        assert metrics.sample_sex(bam) == "Male"
    assert idxstats.call_count == 1

    fact = SampleWorkspace(folder).detected("sex", bam)
    assert fact is not None and fact.value == "Male"
    assert (fact.method, fact.confidence, fact.file) == (
        "chrY/chrX mapped-read ratio",
        "high",
        "s.bam",
    )
    assert fact.by and fact.by.startswith("wgsextract ") and fact.at


@needs_htslib
def test_a_replaced_file_is_detected_again(seqs, folder):
    bam = _bam(seqs, folder / "s.bam", MALE)
    assert metrics.sample_sex(bam) == "Male"
    # Realigned into the same name, now a different sample's reads.
    _bam(seqs, folder / "s.bam", FEMALE)
    with _idxstats_counter() as idxstats:
        assert metrics.sample_sex(bam) == "Female"
    assert idxstats.call_count == 1
    assert SampleWorkspace(folder).detected("sex", bam).value == "Female"


@needs_htslib
def test_a_genome_run_uses_its_folder_for_inputs_kept_elsewhere(seqs, tmp_path):
    genome, elsewhere = tmp_path / "genome", tmp_path / "reads"
    genome.mkdir()
    elsewhere.mkdir()
    bam = _bam(seqs, elsewhere / "s.bam", MALE)
    with active_workspace(genome, [bam]), _idxstats_counter() as idxstats:
        metrics.sample_sex(bam)
        metrics.sample_sex(bam)
    assert idxstats.call_count == 1
    assert SampleWorkspace(genome).detected("sex", bam).file == str(Path(bam))
    assert not (elsewhere / WORKSPACE_FILE).exists()


@needs_htslib
def test_override_wins_and_changes_the_ploidy_bcftools_gets(seqs, folder):
    bam = _bam(seqs, folder / "s.bam", FEMALE)
    assert metrics.sample_sex(bam) == "Female"
    with sex_samples_args(bam, str(folder), ["--ploidy", "GRCh38"]) as args:
        assert args == []

    code, _ = _cli("sample", "set", "--input", bam, "--sex", "m")
    assert code == 0
    with _idxstats_counter() as idxstats:
        with sex_samples_args(bam, str(folder), ["--ploidy", "GRCh38"]) as args:
            assert args[0] == "--samples-file"
            assert Path(args[1]).read_text().endswith("\tM\n")
    assert idxstats.call_count == 0

    code, out = _cli("--json", "sample", "show", "--input", bam)
    shown = json.loads(out)["details"]
    assert shown["effective"]["sex"] == {
        "name": "sex",
        "value": "Male",
        "source": "override",
    }
    # The detected call is still on record, for comparison.
    assert shown["files"]["s.bam"]["facts"]["sex"]["value"] == "Female"

    assert _cli("sample", "unset", "--workspace", str(folder), "--sex")[0] == 0
    assert metrics.sample_sex(bam) == "Female"


@needs_htslib
def test_platform_override(seqs, folder):
    bam = _bam(seqs, folder / "s.bam", FEMALE)
    assert input_platform(bam) == "illumina"
    recorded = SampleWorkspace(folder).detected("platform", bam)
    assert recorded.method == "first read name pattern"
    api.sample.set_overrides(api.SampleSetParams(input=Path(bam), platform="nanopore"))
    assert input_platform(bam) == "ont"
    # A platform is a file's: setting one without the file is refused.
    with pytest.raises(api.CommandFailed, match="--input"):
        api.sample.set_overrides(api.SampleSetParams(workspace=folder, platform="ont"))


@needs_htslib
def test_info_records_what_later_commands_read(seqs, folder):
    bam = _bam(seqs, folder / "s.bam", MALE)
    assert _cli("info", "--detailed", "--input", bam)[0] == 0
    workspace = SampleWorkspace(folder)
    facts = workspace.describe(bam)["files"]["s.bam"]["facts"]
    assert {"sex", "platform", "read_length", "mean_coverage"} <= set(facts)
    assert facts["sex"]["by"].endswith(" info")

    # Without info's own cache, the sex comes from the workspace alone.
    Path(f"{bam}.wgse_info.json").unlink()
    with _idxstats_counter() as idxstats:
        assert metrics.sample_sex(bam) == "Male"
    assert idxstats.call_count == 0
    first = genotype_qc.target_depths(bam, str(folder))
    with patch.object(genotype_qc, "_counted_depths", side_effect=AssertionError):
        again = genotype_qc.target_depths(bam, str(folder))
    assert first and again and again[0] == pytest.approx(first[0], abs=0.01)
    assert again[1].startswith(WORKSPACE_FILE)

    raw = sample_workspace.tomllib.loads((folder / WORKSPACE_FILE).read_text())
    jsonschema.validate(raw, sample_workspace.load_schema())
    # A mean of mixed-length reads, as from long or trimmed reads.
    raw["detected"]["s.bam"]["read_length"]["value"] = 149.5
    jsonschema.validate(raw, sample_workspace.load_schema())


def test_depths_missing_a_target_are_estimated_again(folder):
    # A CRAM read without its index can't tell which targets some reads are on.
    reads = folder / "s.cram"
    reads.write_bytes(b"reads")
    partial = {"autosomes": 30.0, "chrX": 15.0}
    with patch.object(
        genotype_qc, "_counted_depths", return_value=(partial, "CRAM containers")
    ) as estimate:
        assert genotype_qc.target_depths(str(reads))[0] == partial
        assert genotype_qc.target_depths(str(reads))[0] == partial
    assert estimate.call_count == 2


def test_sample_commands_need_neither_the_reads_nor_the_output_folder(folder):
    reads = folder / "s.bam"
    reads.write_bytes(b"not a BAM")
    blocked = folder / "a-file"
    blocked.write_text("")
    code, out = _cli(
        "sample",
        "set",
        "--input",
        str(reads),
        "--outdir",
        str(blocked / "out"),
        "--sex",
        "female",
    )
    assert code == 0, out
    shown = api.sample.show(api.SampleShowParams(input=reads))
    assert shown.details["overrides"] == {"sex": "Female"}


def test_api_set_show_unset(folder):
    reads = folder / "s.bam"
    reads.write_bytes(b"reads")
    res = api.sample.set_overrides(
        api.SampleSetParams(input=reads, sex="f", platform="hifi")
    )
    assert res.details["overrides"] == {"sex": "Female"}
    assert res.details["files"]["s.bam"]["overrides"]["values"] == {
        "platform": "pacbio"
    }
    assert {k: v["value"] for k, v in res.details["effective"].items()} == {
        "sex": "Female",
        "platform": "pacbio",
    }
    api.sample.unset_overrides(api.SampleUnsetParams(workspace=folder, sex=True))
    shown = api.sample.show(api.SampleShowParams(input=reads)).details
    assert shown["overrides"] == {}
    assert {k: v["value"] for k, v in shown["effective"].items()} == {
        "platform": "pacbio"
    }
    api.sample.unset_overrides(api.SampleUnsetParams(input=reads, platform=True))
    raw = sample_workspace.tomllib.loads((folder / WORKSPACE_FILE).read_text())
    assert "override" not in raw
    with pytest.raises(api.UsageError):
        api.SampleSetParams(workspace=folder, sex="unknown")
    with pytest.raises(api.UsageError):
        api.SampleShowParams()


@pytest.mark.parametrize(
    "text",
    [
        "schema = [unterminated\n",
        'schema = "wgsextract.sample/2"\n[override]\nsex = "Male"\n',
        'schema = "something.else/1"\n',
        'schema = "wgsextract.sample/1"\ndetected = "broken"\n',
        'schema = "wgsextract.sample/1"\n[outputs]\n"x.manifest.json" = 1\n',
    ],
    ids=["corrupt", "newer", "foreign", "detected-not-a-table", "bad-output"],
)
@needs_htslib
def test_unusable_workspace_is_ignored_and_left_alone(seqs, folder, text, caplog):
    (folder / WORKSPACE_FILE).write_text(text)
    bam = _bam(seqs, folder / "s.bam", FEMALE)
    with _idxstats_counter() as idxstats:
        assert metrics.sample_sex(bam) == "Female"
        assert metrics.sample_sex(bam) == "Female"
    assert idxstats.call_count == 2
    assert (folder / WORKSPACE_FILE).read_text() == text
    assert f"Ignoring {folder / WORKSPACE_FILE}" in caplog.text
    code, _ = _cli("sample", "set", "--workspace", str(folder), "--sex", "Male")
    assert code == 1
    assert (folder / WORKSPACE_FILE).read_text() == text


@needs_htslib
def test_older_workspace_keeps_overrides_and_redetects(seqs, folder):
    bam = _bam(seqs, folder / "s.bam", FEMALE)
    fp = sample_workspace.file_fingerprint(bam)
    (folder / WORKSPACE_FILE).write_text(
        '[override]\nplatform = "ont"\n'
        f'[detected."s.bam"]\nfingerprint = "{fp}"\n'
        '[detected."s.bam".sex]\nvalue = "Male"\nversion = 2\n'
    )
    with _idxstats_counter() as idxstats:
        assert metrics.sample_sex(bam) == "Female"
    assert idxstats.call_count == 1
    raw = sample_workspace.tomllib.loads((folder / WORKSPACE_FILE).read_text())
    assert raw["schema"] == sample_workspace.SCHEMA_ID
    assert raw["override"] == {"platform": "ont"}
    assert raw["detected"]["s.bam"]["sex"]["value"] == "Female"


@needs_htslib
def test_a_fact_from_an_older_detector_is_shown_but_not_used(seqs, folder):
    bam = _bam(seqs, folder / "s.bam", FEMALE)
    fp = sample_workspace.file_fingerprint(bam)
    (folder / WORKSPACE_FILE).write_text(
        f'schema = "{sample_workspace.SCHEMA_ID}"\n'
        f'[detected."s.bam"]\nfingerprint = "{fp}"\n'
        '[detected."s.bam".sex]\nvalue = "Male"\nmethod = "old"\n'
        'confidence = "high"\nversion = 1\nby = "wgsextract 0.1"\n'
        'at = "2020-01-01T00:00:00+00:00"\n'
    )
    details = api.sample.show(api.SampleShowParams(input=Path(bam))).details
    assert "sex" not in details["effective"]
    assert details["files"]["s.bam"]["facts"]["sex"]["value"] == "Male"
    with _idxstats_counter() as idxstats:
        assert metrics.sample_sex(bam) == "Female"
    assert idxstats.call_count == 1


@needs_htslib
def test_invalid_override_is_ignored(seqs, folder, caplog):
    (folder / WORKSPACE_FILE).write_text(
        f'schema = "{sample_workspace.SCHEMA_ID}"\n[override]\nsex = "Robot"\n'
    )
    bam = _bam(seqs, folder / "s.bam", MALE)
    assert metrics.sample_sex(bam) == "Male"
    assert "ignoring override" in caplog.text


@needs_htslib
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX folder permissions")
def test_read_only_folder_never_fails_a_command(seqs, folder):
    bam = _bam(seqs, folder / "s.bam", MALE)
    folder.chmod(0o555)
    try:
        assert metrics.sample_sex(bam) == "Male"
    finally:
        folder.chmod(0o755)
    assert not (folder / WORKSPACE_FILE).exists()


@needs_htslib
def test_inputs_outside_sample_folders_are_not_recorded(seqs, tmp_path):
    bam = _bam(seqs, tmp_path / "s.bam", MALE)
    with _idxstats_counter() as idxstats:
        metrics.sample_sex(bam)
        metrics.sample_sex(bam)
    assert idxstats.call_count == 2
    assert not (tmp_path / WORKSPACE_FILE).exists()


@needs_htslib
def test_an_override_in_a_shared_folder_is_for_that_file_alone(seqs, tmp_path):
    # Downloads: two people's BAMs side by side.
    dad = _bam(seqs, tmp_path / "dad.bam", MALE)
    mom = _bam(seqs, tmp_path / "mom.bam", FEMALE)
    code, out = _cli("sample", "set", "--input", mom, "--sex", "f", "--platform", "ont")
    assert code == 0, out
    assert (metrics.sample_sex(mom), input_platform(mom)) == ("Female", "ont")
    assert (metrics.sample_sex(dad), input_platform(dad)) == ("Male", "illumina")

    # A folder-wide sex, written by hand, isn't trusted here either.
    raw = sample_workspace.tomllib.loads((tmp_path / WORKSPACE_FILE).read_text())
    assert set(raw["override"]) == {"mom.bam"}
    jsonschema.validate(raw, sample_workspace.load_schema())
    raw["override"]["sex"] = "Female"
    (tmp_path / WORKSPACE_FILE).write_text(sample_workspace.tomli_w.dumps(raw))
    assert metrics.sample_sex(dad) == "Male"

    # Nor is mom's override, once her file is replaced by other reads.
    _bam(seqs, tmp_path / "mom.bam", MALE)
    assert metrics.sample_sex(mom) == "Male"
    assert input_platform(mom) == "illumina"


def test_a_sex_override_outside_a_genome_folder_needs_the_file(tmp_path):
    with pytest.raises(api.CommandFailed, match="isn't a genome folder"):
        api.sample.set_overrides(api.SampleSetParams(workspace=tmp_path, sex="m"))


@needs_htslib
def test_an_override_the_reads_contradict_is_warned_about(seqs, folder):
    bam = _bam(seqs, folder / "s.bam", FEMALE)
    api.sample.set_overrides(
        api.SampleSetParams(input=Path(bam), sex="male", platform="ont")
    )
    out = folder / "out"
    code, text = _cli(
        "--json", "extract", "ydna-bam", "--input", bam, "--outdir", str(out)
    )
    assert code == 0, text
    warnings = {w["code"]: w for w in json.loads(text)["warnings"]}
    assert warnings["sex_override_differs"]["details"] == {
        "file": "s.bam",
        "detected": "Female",
        "override": "Male",
    }
    manifest = json.loads(next(out.glob("*.manifest.json")).read_text())
    assert "sex_override_differs" in {w["code"] for w in manifest["warnings"]}

    # Once detected, the fact is read back, not detected again, but still
    # checked (in a later run).
    later = CommandResult(command="vcf snp")
    with (
        _idxstats_counter() as idxstats,
        patch.object(sample_workspace, "_checked", set()),
        collecting(later),
    ):
        assert metrics.sample_sex(bam) == "Male"
        assert input_platform(bam) == "ont"
    assert idxstats.call_count == 0
    assert {w.code for w in later.warnings} == {
        "sex_override_differs",
        "platform_override_differs",
    }
    res = api.sample.show(api.SampleShowParams(input=Path(bam)))
    assert res.details["files"]["s.bam"]["facts"]["sex"]["value"] == "Female"
