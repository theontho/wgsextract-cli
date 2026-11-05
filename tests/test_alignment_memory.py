from pathlib import Path
from types import SimpleNamespace

import pytest

from wgsextract_cli.core import alignment_memory, results
from wgsextract_cli.core import resource_estimates as resources
from wgsextract_cli.core.utils import WGSExtractError

GIB = 1024**3
ULTIMA_READS = 313_736_048


def test_stream_collation_can_make_an_otherwise_fitting_mem2_pipeline_unsafe(
    tmp_path,
    monkeypatch,
):
    ref = tmp_path / "ref.fa"
    ref.write_text(">chr1\nA\n")
    Path(str(ref) + ".fai").write_text("chr1\t1000000\t0\t1\t2\n")
    options = {"threads": "2", "memory": "100M", "reads": 10000, "samblaster": True}
    baseline = alignment_memory.estimate_mapping_memory(str(ref), "bwa-mem2", **options)
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: baseline.total)
    alignment_memory.check_mapping_memory(str(ref), "bwa-mem2", **options)
    with pytest.raises(WGSExtractError, match="collation"):
        alignment_memory.check_mapping_memory(
            str(ref),
            "bwa-mem2",
            additional_ram_bytes=100 * 1024**2,
            **options,
        )
    estimate = resources.estimate_resources(
        "realign",
        input_paths=[],
        output_dir=str(tmp_path),
        reference=str(ref),
        aligner="bwa-mem2",
        additional_ram_bytes=100 * 1024**2,
        **options,
    )
    assert estimate.resources[0].status == "LOW"
    assert estimate.resources[0].components["collation"] == 100 * 1024**2


@pytest.mark.parametrize(
    ("threads", "bwa_peak", "unit_peak", "status"),
    [("2", 9.179, 10.639, "OK"), ("4", 14.695, 16, "LOW")],
)
def test_ultima_preflight_covers_measured_hg008_memory(
    tmp_path, monkeypatch, caplog, threads, bwa_peak, unit_peak, status
):
    ref = tmp_path / "hs38DH.fa"
    ref.write_text(">chr1\nA\n")
    Path(str(ref) + ".fai").write_text("chr1\t3217346917\t0\t1\t2\n")
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 16 * GIB)
    estimate = resources.estimate_resources(
        "realign",
        input_paths=[],
        output_dir=str(tmp_path),
        reference=str(ref),
        aligner="bwa",
        threads=threads,
        memory="256M",
        reads=ULTIMA_READS,
        platform="ultima",
        samblaster=False,
    )
    ram = estimate.resources[0]
    assert (
        ram.components["index_resident"] + ram.components["working"] >= bwa_peak * GIB
    )
    assert ram.estimated_bytes >= unit_peak * GIB
    assert ram.status == status
    alignment_memory.check_mapping_memory(
        str(ref),
        "bwa",
        threads=threads,
        memory="256M",
        reads=ULTIMA_READS,
        platform="ultima",
        samblaster=False,
    )
    if status == "LOW":
        assert "requested settings are unchanged" in caplog.text
        assert "--threads 3" in caplog.text
        assert "--threads 3 --memory 256M" in caplog.text
        assert "Continuing with bwa" in caplog.text
    else:
        assert "Continuing with bwa" not in caplog.text


def test_read_length_and_batch_size_scale_bwa_but_leave_mem2_calibration_unchanged():
    options = {
        "threads": "4",
        "samblaster": False,
        "reads": ULTIMA_READS,
        "platform": "ultima",
    }
    normal = alignment_memory.estimate_mapping_memory(None, "bwa", **options)
    longer = alignment_memory.estimate_mapping_memory(
        None, "bwa", read_length=450, **options
    )
    sampled = alignment_memory.estimate_mapping_memory(
        None, "bwa", read_length=291, **options
    )
    tiny = alignment_memory.estimate_mapping_memory(
        None, "bwa", **{**options, "reads": 6676}, read_length=87
    )
    assert longer.working > normal.working >= sampled.working > tiny.working
    assert sampled.index_resident + sampled.working > 14.695 * GIB
    legacy = alignment_memory.estimate_mapping_memory(None, "bwa-mem2", threads="4")
    assert (
        alignment_memory.estimate_mapping_memory(
            None, "bwa-mem2", threads="4", read_length=450, platform="ultima"
        )
        == legacy
    )


@pytest.mark.parametrize("read_length", [0, -1, float("nan"), float("inf")])
def test_invalid_read_lengths_fail_instead_of_reporting_false_safe(read_length):
    with pytest.raises(ValueError, match="read_length"):
        alignment_memory.estimate_mapping_memory(
            None, "bwa", platform="ultima", read_length=read_length
        )


@pytest.mark.parametrize(
    ("host_available", "child_limit", "child_current", "child_cache", "expected"),
    [
        (64, "max", 0, 0, 11),
        (7, "max", 0, 0, 7),
        (64, "8", 7, 6, 7),
        (64, "8", 9, 0, 0),
    ],
)
def test_available_ram_honors_cgroup_ancestors_and_reclaimable_file_cache(
    tmp_path,
    monkeypatch,
    host_available,
    child_limit,
    child_current,
    child_cache,
    expected,
):
    membership = tmp_path / "membership"
    membership.write_text("0::/parent/child\n")
    root = tmp_path / "cgroup"
    child = root / "parent" / "child"
    child.mkdir(parents=True)
    for group, limit, current, cache in [
        (root, "max", 0, 0),
        (child.parent, str(16 * GIB), 6 * GIB, GIB),
        (
            child,
            child_limit if child_limit == "max" else str(int(child_limit) * GIB),
            child_current * GIB,
            child_cache * GIB,
        ),
    ]:
        (group / "memory.max").write_text(limit)
        (group / "memory.current").write_text(str(current))
        (group / "memory.stat").write_text(f"inactive_file {cache}\n")
    budget = alignment_memory._cgroup_available_memory(membership, root)
    monkeypatch.setattr(alignment_memory, "_cgroup_available_memory", lambda: budget)
    monkeypatch.setattr(
        alignment_memory.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=host_available * GIB),
    )
    assert alignment_memory.available_memory() == expected * GIB


def test_parent_relative_membership_retains_readable_mounted_root_limit(tmp_path):
    membership = tmp_path / "membership"
    membership.write_text("0::/../../outside\n")
    (tmp_path / "memory.max").write_text(str(16 * GIB))
    (tmp_path / "memory.current").write_text(str(4 * GIB))
    assert alignment_memory._cgroup_available_memory(membership, tmp_path) == 12 * GIB


def test_subtree_mount_offset_retains_tighter_descendant_limit(tmp_path):
    root = tmp_path / "mounted cgroup"
    worker = root / "worker"
    worker.mkdir(parents=True)
    membership = tmp_path / "membership"
    membership.write_text("0::/host/container/worker\n")
    mountinfo = tmp_path / "mountinfo"
    point = str(root).replace("\\", r"\134").replace(" ", r"\040")
    mountinfo.write_text(f"31 22 0:28 /host/container {point} rw - cgroup2 cgroup rw\n")
    for group, limit, current in [(root, 16, 4), (worker, 4, 1)]:
        (group / "memory.max").write_text(str(limit * GIB))
        (group / "memory.current").write_text(str(current * GIB))
    assert (
        alignment_memory._cgroup_available_memory(membership, root, mountinfo)
        == 3 * GIB
    )


def test_namespaced_cgroup_root_and_unavailable_limits(tmp_path):
    membership = tmp_path / "membership"
    membership.write_text("0::/host/path/not/mounted\n")
    (tmp_path / "memory.max").write_text(str(16 * GIB))
    (tmp_path / "memory.current").write_text(str(4 * GIB))
    assert alignment_memory._cgroup_available_memory(membership, tmp_path) == 12 * GIB
    (tmp_path / "memory.max").write_text("max")
    assert alignment_memory._cgroup_available_memory(membership, tmp_path) is None
    assert (
        alignment_memory._cgroup_available_memory(tmp_path / "missing", tmp_path)
        is None
    )


def _hs38(tmp_path: Path) -> str:
    ref = tmp_path / "hs38.fa"
    ref.write_text(">chr1\nA\n")
    Path(str(ref) + ".fai").write_text("chr1\t3099922541\t0\t1\t2\n")
    return str(ref)


@pytest.mark.parametrize(
    ("preset", "build_peak"),
    [("map-ont", 11.17), ("map-hifi", 11.11), ("lr:hq", 11.11), ("map-pb", 8.62),
     ("sr", 11.31), (None, 11.31)],
)  # fmt: skip
def test_minimap2_estimate_covers_measured_hs38_peaks(tmp_path, preset, build_peak):
    """#428: minimap2 2.31 builds the hs38 index in memory on every run."""
    estimate = alignment_memory.estimate_mapping_memory(
        _hs38(tmp_path), "minimap2", threads="4", memory="512M",
        samblaster=False, preset=preset,
    )  # fmt: skip
    assert estimate.index_resident + estimate.working >= build_peak * GIB
    if preset == "map-ont":
        # 40k HG002 ONT reads mapped at -t 4 peaked at 10.93 GiB; the #299
        # chr20 realign at --threads 4 --memory 512M at 14.8 GB.
        assert estimate.index_resident + estimate.working >= 10.93 * GIB
        assert estimate.total >= 14.8e9


def test_minimap2_preflight_suggests_settings_that_fit_a_14g_cgroup(
    tmp_path, monkeypatch
):
    """#428: the #299 ONT realign at --threads 4 and default --memory was
    OOM-killed in a 14 GiB cgroup with no warning beforehand."""
    ref = _hs38(tmp_path)
    options = {"samblaster": False, "preset": "map-ont"}
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 14 * GIB)
    result = results.CommandResult("align")
    with results.collecting(result):
        alignment_memory.check_mapping_memory(
            ref, "minimap2", threads="4", memory="1G", **options
        )
    [warning] = result.warnings
    assert warning.code == "alignment_memory"
    assert warning.details["available_gib"] == 14
    assert warning.details["estimated_gib"] > 14
    threads = warning.details["suggested_threads"]
    memory = warning.details["suggested_memory"]
    assert f"--threads {threads} --memory {memory}" in warning.message
    fitted = alignment_memory.estimate_mapping_memory(
        ref, "minimap2", threads=str(threads), memory=str(memory), **options
    )
    assert fitted.total <= 14 * GIB

    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 10 * GIB)
    result = results.CommandResult("align")
    with results.collecting(result):
        alignment_memory.check_mapping_memory(
            ref, "minimap2", threads="4", memory="1G", **options
        )
    [warning] = result.warnings
    assert warning.details["suggested_threads"] is None
    assert "Even one thread exceeds" in warning.message


def test_minimap2_small_short_read_input_does_not_get_the_genome_fallback():
    """A few-MB FASTQ pair aligned with minimap2 sr and samblaster must not be
    budgeted (and warned about) as the 680-million-read genome fallback."""
    options = {"threads": "2", "memory": "256M", "samblaster": True, "preset": "sr"}
    small = alignment_memory.estimate_mapping_memory(
        None, "minimap2", input_bytes=5_000_000, **options
    )
    unknown = alignment_memory.estimate_mapping_memory(None, "minimap2", **options)
    assert small.samblaster < 10 * 1024**2 < GIB < unknown.samblaster
