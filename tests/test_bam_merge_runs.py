"""``bam merge`` of several flow-cell runs of one sample (#339)."""

import random
import uuid
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools, sam_to_bam, write_fasta
from wgsextract_cli import api
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import run_command

needs_samtools = needs_tools("samtools")

_GENOME = {
    name: "".join(random.Random(name).choices("ACGT", k=2000))
    for name in ("chr1", "chr2")
}
_SQ = [f"@SQ\tSN:{name}\tLN:{len(seq)}" for name, seq in _GENOME.items()]


def _view(path: Path | str, *args: str) -> list[str]:
    samtools = get_tool_path("samtools")
    out = run_command([samtools, "view", *args, str(path)], capture_output=True)
    return out.stdout.splitlines()


def _run(
    path: Path,
    seed: int,
    *,
    rg: str = "",
    mods: bool = True,
    names: list[str] | None = None,
    flag: int = 0,
    tag_reads: bool = True,
    sq: list[str] = _SQ,
    length: int = 100,
) -> Path:
    """An ONT-like run: 40 reads of ``length`` bp, UUID names, MM/ML tags unless
    ``mods`` is false, and an RG tag naming ``rg`` unless ``tag_reads`` is."""
    rng = random.Random(seed)
    lines = ["@HD\tVN:1.6\tSO:coordinate", *sq, *([rg] if rg else [])]
    rg_tag = "\tRG:Z:" + rg.split("\t")[1][3:] if rg and tag_reads else ""
    for i in range(40):
        contig = rng.choice(list(_GENOME))
        pos = rng.randrange(1, 2001 - length)
        seq = _GENOME[contig][pos - 1 : pos - 1 + length]
        name = names[i] if names else str(uuid.UUID(int=rng.getrandbits(128)))
        tags = "\tMM:Z:C+m?,0;\tML:B:C,200" if mods else ""
        lines.append(
            f"{name}\t{flag}\t{contig}\t{pos}\t60\t{length}M\t*\t0\t0\t{seq}\t{'I' * length}"
            f"{tags}{rg_tag}"
        )
    sam_to_bam(lines, path)
    return path


def _merge(tmp_path: Path, inputs, **fields) -> api.Result:
    return api.bam.merge(
        api.BamMergeParams(inputs=tuple(inputs), outdir=tmp_path / "out", **fields)
    )


def _merged(result: api.Result) -> str:
    (output,) = [o for o in result.outputs if o.role == "merged_alignment"]
    return str(output.path)


def _groups(path: Path | str) -> dict[str, dict[str, str]]:
    groups = [
        dict(f.split(":", 1) for f in line.split("\t")[1:])
        for line in _view(path, "-H", "--no-PG")
        if line.startswith("@RG")
    ]
    return {g["ID"]: g for g in groups}


def _reads(path: Path | str, *args: str) -> dict[str, list[str]]:
    return {line.split("\t")[0]: line.split("\t") for line in _view(path, *args)}


@needs_samtools
@pytest.mark.parametrize("cram", [False, True])
def test_runs_keep_their_read_groups_sample_and_mod_tags(tmp_path, cram):
    run1 = _run(tmp_path / "run1.bam", 1)
    run2 = _run(tmp_path / "run2.bam", 2)
    ref = write_fasta(tmp_path / "ref.fa", _GENOME) if cram else None
    result = _merge(tmp_path, [run1, run2], sample="HG002", cram=cram, ref=ref)

    merged_path = _merged(result)
    assert _groups(merged_path) == {
        run: {"ID": run, "SM": "HG002", "PL": "ONT"} for run in ("run1", "run2")
    }
    merged = _reads(merged_path, *(["--reference", str(ref)] if cram else []))
    for run in (run1, run2):
        for name, fields in _reads(run).items():
            assert f"RG:Z:{run.stem}" in merged[name]
            mods = [t for t in fields if t[:2] in ("MM", "ML")]
            # contract: methylation calls survive the merge unchanged.
            assert mods and mods == [t for t in merged[name] if t[:2] in ("MM", "ML")]
    assert result.metrics["read_groups"] == 2
    runs = [r["autosomal_depth"] for r in result.details["runs"]]
    assert result.metrics["autosomal_depth"] == pytest.approx(sum(runs), abs=0.02)
    assert not result.warnings


@needs_samtools
def test_runs_of_different_samples_are_refused_unless_named(tmp_path):
    alice = _run(tmp_path / "a.bam", 1, rg="@RG\tID:fc1\tSM:alice\tPL:ONT")
    bob = _run(tmp_path / "b.bam", 2, rg="@RG\tID:fc2\tSM:Alice\tPL:ONT\tPU:FAB2")
    with pytest.raises(api.CommandFailed, match="name different samples"):
        _merge(tmp_path, [alice, bob])
    assert not list((tmp_path / "out").glob("*.bam"))

    result = _merge(tmp_path, [alice, bob], sample="alice")
    assert _groups(_merged(result)) == {
        "fc1": {"ID": "fc1", "SM": "alice", "PL": "ONT"},
        "fc2": {"ID": "fc2", "SM": "alice", "PL": "ONT", "PU": "FAB2"},
    }


@needs_samtools
def test_runs_without_a_sample_name_take_the_output_name(tmp_path):
    a = _run(tmp_path / "a.bam", 1)
    b = _run(tmp_path / "b.bam", 2)
    result = _merge(tmp_path, [a, b], output="NA12878.bam")
    assert [w.code for w in result.warnings] == ["sample_name_assumed"]
    assert {g["SM"] for g in _groups(_merged(result)).values()} == {"NA12878"}


@needs_samtools
def test_clashing_read_group_ids_stay_apart_with_the_sample_set(tmp_path):
    # Both runs call their read group "1", as many pipelines do.
    a = _run(tmp_path / "a.bam", 1, rg="@RG\tID:1\tPU:FAA1")
    b = _run(tmp_path / "b.bam", 2, rg="@RG\tID:1\tPU:FAB2")
    result = _merge(tmp_path, [a, b], sample="S")
    assert [w.code for w in result.warnings] == ["read_group_conflict"]
    groups = _groups(_merged(result))
    assert sorted(g["PU"] for g in groups.values()) == ["FAA1", "FAB2"]
    assert all(g["SM"] == "S" and g["PL"] == "ONT" for g in groups.values())
    merged = _reads(_merged(result))
    for run, unit in ((a, "FAA1"), (b, "FAB2")):
        for name in _reads(run):
            rg = next(t for t in merged[name] if t.startswith("RG:Z:"))[5:]
            assert groups[rg]["PU"] == unit


@needs_samtools
def test_runs_with_identical_read_group_lines_stay_apart(tmp_path):
    # Review of #465: minimap2 -R '@RG\tID:x\tPL:ONT' for each flow cell
    # gave one read group for both runs, with no warning.
    a = _run(tmp_path / "a.bam", 1, rg="@RG\tID:x\tPL:ONT")
    b = _run(tmp_path / "b.bam", 2, rg="@RG\tID:x\tPL:ONT")
    result = _merge(tmp_path, [a, b], sample="S")
    assert [w.code for w in result.warnings] == ["read_group_renamed"]
    assert result.metrics["read_groups"] == 2
    merged = _reads(_merged(result))
    ids = [
        {next(t for t in merged[name] if t.startswith("RG:Z:")) for name in _reads(r)}
        for r in (a, b)
    ]
    assert all(len(found) == 1 for found in ids) and ids[0] != ids[1]
    # The seeded suffix names the same read groups on every run.
    again = _merge(tmp_path, [a, b], sample="S", output="again.bam")
    assert _groups(_merged(again)) == _groups(_merged(result))


@needs_samtools
def test_one_run_split_across_files_keeps_one_read_group(tmp_path):
    rg = "@RG\tID:x\tPL:ONT\tPU:FAA1"
    a = _run(tmp_path / "a.bam", 1, rg=rg)
    b = _run(tmp_path / "b.bam", 2, rg=rg)
    result = _merge(tmp_path, [a, b], sample="S")
    assert not result.warnings
    assert list(_groups(_merged(result))) == ["x"]


@needs_samtools
def test_templates_spanning_files_keep_one_read_group(tmp_path):
    # One paired run split by region: mates share names across the files.
    names = [f"read{i}" for i in range(40)]
    rg = "@RG\tID:x\tSM:S\tPL:ILLUMINA"
    a = _run(tmp_path / "a.bam", 1, names=names, rg=rg)
    b = _run(tmp_path / "b.bam", 2, names=names, rg=rg)
    result = _merge(tmp_path, [a, b])
    assert "read_group_renamed" not in [w.code for w in result.warnings]
    assert list(_groups(_merged(result))) == ["x"]


@needs_samtools
def test_a_run_included_twice_is_refused(tmp_path):
    run1 = _run(tmp_path / "run1.bam", 1)
    again = tmp_path / "run1_copy.bam"
    again.write_bytes(run1.read_bytes())
    with pytest.raises(api.CommandFailed, match="the same reads are in both"):
        _merge(tmp_path, [run1, again])


@needs_samtools
def test_different_reads_sharing_names_are_flagged(tmp_path):
    names = [f"read{i}" for i in range(40)]
    a = _run(tmp_path / "a.bam", 1, names=names)
    b = _run(tmp_path / "b.bam", 2, names=names)
    result = _merge(tmp_path, [a, b], sample="S")
    assert [w.code for w in result.warnings] == ["read_name_collision"]


@needs_samtools
def test_runs_of_different_platforms_are_refused(tmp_path):
    ont = _run(tmp_path / "ont.bam", 1, rg="@RG\tID:a\tSM:S\tPL:ONT")
    illumina = _run(tmp_path / "ilmn.bam", 2, rg="@RG\tID:b\tSM:S\tPL:ILLUMINA")
    with pytest.raises(api.CommandFailed, match="different sequencing platforms"):
        _merge(tmp_path, [ont, illumina])


@needs_samtools
def test_a_run_without_mod_tags_is_flagged(tmp_path):
    result = _merge(
        tmp_path,
        [_run(tmp_path / "mods.bam", 1), _run(tmp_path / "plain.bam", 2, mods=False)],
        sample="S",
    )
    (warning,) = result.warnings
    assert warning.code == "mod_tags_partial"
    assert warning.details["inputs"] == "plain.bam"


@needs_samtools
def test_runs_on_references_with_other_sequences_are_refused(tmp_path):
    a = _run(tmp_path / "a.bam", 1)
    b = _run(tmp_path / "b.bam", 2)
    samtools = get_tool_path("samtools")
    for path, md5 in ((a, "0" * 32), (b, "1" * 32)):
        sam = path.with_suffix(".h.sam")
        sam.write_text(
            "".join(
                line + (f"\tM5:{md5}" if line.startswith("@SQ") else "") + "\n"
                for line in _view(path, "-H", "--no-PG")
            )
        )
        fixed = path.with_suffix(".m5.bam")
        with open(fixed, "wb") as f:
            run_command([samtools, "reheader", str(sam), str(path)], stdout=f)
        fixed.replace(path)
    with pytest.raises(api.CommandFailed, match="different sequence dictionary"):
        _merge(tmp_path, [a, b])


@needs_samtools
def test_a_run_basecalled_again_is_refused(tmp_path):
    # Review of #465: ONT read IDs name molecules, so a second basecall of a
    # run shares them though its sequences differ.
    names = [str(uuid.UUID(int=i)) for i in range(40)]
    a = _run(tmp_path / "a.bam", 1, names=names)
    b = _run(tmp_path / "b.bam", 2, names=names)
    with pytest.raises(api.CommandFailed, match="same reads, basecalled again"):
        _merge(tmp_path, [a, b])


@needs_samtools
def test_mates_sharing_a_name_across_runs_are_flagged(tmp_path):
    # Review of #465: the warning compared (name, mate) pairs.
    names = [f"read{i}" for i in range(40)]
    a = _run(tmp_path / "a.bam", 1, names=names, flag=64 | 1 | 8)
    b = _run(tmp_path / "b.bam", 2, names=names, flag=128 | 1 | 8)
    result = _merge(tmp_path, [a, b], sample="S")
    assert [w.code for w in result.warnings] == ["read_name_collision"]


@needs_samtools
def test_same_file_names_in_different_folders_are_all_checked(tmp_path):
    # Review of #465: checks keyed by file name let the second hide the first.
    (tmp_path / "fcA").mkdir()
    (tmp_path / "fcB").mkdir()
    a = _run(tmp_path / "fcA" / "calls.bam", 1, rg="@RG\tID:a\tSM:alice")
    b = _run(tmp_path / "fcB" / "calls.bam", 2, rg="@RG\tID:b\tSM:bob")
    with pytest.raises(api.CommandFailed, match="name different samples"):
        _merge(tmp_path, [a, b])


@needs_samtools
def test_an_input_mixing_platforms_is_refused(tmp_path):
    mixed = _run(tmp_path / "mixed.bam", 1, rg="@RG\tID:a\tSM:S\tPL:ONT")
    rgs = "@RG\tID:a\tSM:S\tPL:ONT\n@RG\tID:b\tSM:S\tPL:ILLUMINA"
    header = "".join(
        line + "\n" for line in _view(mixed, "-H", "--no-PG") if line[:3] != "@RG"
    )
    sam = tmp_path / "mixed.h.sam"
    sam.write_text(header + rgs + "\n", newline="\n")
    fixed = tmp_path / "mixed2.bam"
    with open(fixed, "wb") as f:
        run_command(
            [get_tool_path("samtools"), "reheader", str(sam), str(mixed)], stdout=f
        )
    ont = _run(tmp_path / "ont.bam", 2, rg="@RG\tID:c\tSM:S\tPL:ONT")
    with pytest.raises(api.CommandFailed, match="different sequencing platforms"):
        _merge(tmp_path, [fixed, ont])


@needs_samtools
def test_file_names_samtools_strips_alike_are_refused(tmp_path):
    # Review of #465: samtools strips .bam, then .cram, so both become "run".
    a = _run(tmp_path / "run.cram.bam", 1)
    (tmp_path / "b").mkdir()
    b = _run(tmp_path / "b" / "run.bam", 2)
    with pytest.raises(api.CommandFailed, match="would both get read group 'run'"):
        _merge(tmp_path, [a, b], sample="S")


@needs_samtools
def test_md5s_are_compared_across_every_input(tmp_path):
    # Review of #465: the first input's missing M5 hid the other two's clash.
    sq = [line.split("\t") for line in _SQ]
    with_md5 = [
        ["\t".join([*fields, f"M5:{digit * 32}"]) for fields in sq] for digit in "01"
    ]
    runs = [
        _run(tmp_path / "plain.bam", 1),
        _run(tmp_path / "m0.bam", 2, sq=with_md5[0]),
        _run(tmp_path / "m1.bam", 3, sq=with_md5[1]),
    ]
    with pytest.raises(api.CommandFailed, match="m1.bam has a different sequence"):
        _merge(tmp_path, runs, sample="S")


@needs_samtools
def test_reads_without_a_read_group_are_given_their_file_s(tmp_path):
    # Review of #465: a declared @RG whose reads lack RG tags left them
    # assigned to no run.
    a = _run(tmp_path / "a.bam", 1, rg="@RG\tID:fc1\tSM:S", tag_reads=False)
    b = _run(tmp_path / "b.bam", 2, rg="@RG\tID:fc2\tSM:S")
    merged = _reads(_merged(_merge(tmp_path, [a, b])))
    for run in (a, b):
        for name in _reads(run):
            assert f"RG:Z:{run.stem}" in merged[name]


@needs_samtools
def test_runs_in_a_read_only_folder_still_report_depth(tmp_path):
    # Review of #465: long reads' depth sampling wrote its scratch beside each
    # input.
    runs = tmp_path / "runs"
    runs.mkdir()
    inputs = [_run(runs / f"run{i}.bam", i, length=1000) for i in (1, 2)]
    runs.chmod(0o555)
    try:
        result = _merge(tmp_path, inputs, sample="S")
    finally:
        runs.chmod(0o755)
    assert all(r["autosomal_depth"] for r in result.details["runs"])
