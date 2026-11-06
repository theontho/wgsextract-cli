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
) -> Path:
    """An ONT-like run: 40 reads of 100 bp, UUID names, MM/ML tags unless
    ``mods`` is false."""
    rng = random.Random(seed)
    lines = ["@HD\tVN:1.6\tSO:coordinate", *_SQ, *([rg] if rg else [])]
    rg_tag = "\tRG:Z:" + rg.split("\t")[1][3:] if rg else ""
    for i in range(40):
        contig = rng.choice(list(_GENOME))
        pos = rng.randrange(1, 1900)
        seq = _GENOME[contig][pos - 1 : pos + 99]
        name = names[i] if names else str(uuid.UUID(int=rng.getrandbits(128)))
        tags = "\tMM:Z:C+m?,0;\tML:B:C,200" if mods else ""
        lines.append(
            f"{name}\t0\t{contig}\t{pos}\t60\t100M\t*\t0\t0\t{seq}\t{'I' * 100}"
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
