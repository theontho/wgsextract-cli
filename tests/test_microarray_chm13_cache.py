from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wgsextract_cli.core import microarray_chm13_cache as cache
from wgsextract_cli.core.microarray_chm13 import load_chm13_catalogue


def test_marker_cache_roundtrips_and_rebuilds_corruption(tmp_path: Path) -> None:
    markers = {("chr1", 10): "rs1", ("chrM", 20): "rsMT"}
    build = Mock(return_value=markers)
    assert cache.marker_ids(tmp_path, build) == markers
    assert cache.marker_ids(tmp_path, build) == markers
    assert build.call_count == 1
    (tmp_path / "markers.tsv").write_text("chr1\t10\twrong\n")
    assert cache.marker_ids(tmp_path, build) == markers
    assert build.call_count == 2
    (tmp_path / "markers.tsv.json").unlink()
    assert cache.marker_ids(tmp_path, build) == markers
    assert build.call_count == 3


def test_target_cache_keys_aliases_and_regions_and_requires_index(
    tmp_path: Path,
) -> None:
    def build(work: Path) -> str:
        path = work / "native-targets.tab.gz"
        path.write_bytes(b"target")
        Path(str(path) + ".tbi").write_bytes(b"index")
        return str(path)

    builder = Mock(side_effect=build)
    directory = tmp_path / "cache"
    first = cache.target(directory, {"chr1": "chr1"}, [], builder, tmp_path)
    assert Path(first).read_bytes() == b"target"
    assert cache.target(directory, {"chr1": "chr1"}, [], builder, tmp_path) == first
    assert builder.call_count == 1
    assert cache.target(directory, {"chr1": "1"}, [], builder, tmp_path) != first
    assert (
        cache.target(directory, {"chr1": "chr1"}, [("chr1", 10, 20)], builder, tmp_path)
        != first
    )
    Path(first + ".tbi").write_bytes(b"broken")
    cache.target(directory, {"chr1": "chr1"}, [], builder, tmp_path)
    assert Path(first + ".tbi").read_bytes() == b"index"
    assert builder.call_count == 4
    assert not list(directory.glob("target-*/"))


def test_failed_target_is_not_published(tmp_path: Path) -> None:
    def fail(work: Path) -> str:
        (work / "partial").write_bytes(b"incomplete")
        raise ValueError("interrupted builder")

    with pytest.raises(ValueError, match="interrupted"):
        cache.target(tmp_path, {"chr1": "chr1"}, [], fail, tmp_path)
    assert not list(tmp_path.glob("*.gz"))
    assert not list(tmp_path.glob("target-*/"))


@pytest.mark.parametrize("unavailable", ["lock", "publication"])
def test_unwritable_cache_uses_uncached_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    def denied(*_args):
        raise PermissionError("read-only cache")

    if unavailable == "lock":
        monkeypatch.setattr(cache, "build_lock", denied)
    else:
        monkeypatch.setattr(cache, "_publish", denied)
        monkeypatch.setattr(cache, "_publish_target", denied)
    directory = tmp_path / "cache"
    work = tmp_path / "outputs"
    work.mkdir()
    markers = {("chr1", 10): "rs1"}
    builder = Mock(return_value=markers)
    assert cache.marker_ids(directory, builder) == markers
    builder.assert_called_once()

    def target(stage):
        assert stage == work
        path = stage / "native-targets.tab.gz"
        path.write_bytes(b"target")
        Path(str(path) + ".tbi").write_bytes(b"index")
        return str(path)

    assert cache.target(directory, {"chr1": "chr1"}, [], target, work) == str(
        work / "native-targets.tab.gz"
    )


def test_cache_fallback_does_not_hide_builder_errors(tmp_path: Path) -> None:
    builder = Mock(side_effect=OSError("cannot read installed templates"))
    with pytest.raises(OSError, match="installed templates"):
        cache.marker_ids(tmp_path, builder)
    builder.assert_called_once()
    builder = Mock(side_effect=OSError("target tool failed"))
    with pytest.raises(OSError, match="target tool failed"):
        cache.target(tmp_path, {"chr1": "chr1"}, [], builder, tmp_path)
    builder.assert_called_once()


def test_target_pair_is_not_current_after_interrupted_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "outputs"
    work.mkdir()
    builds = []

    def build(directory):
        builds.append(len(builds) + 1)
        path = directory / "native-targets.tab.gz"
        path.write_bytes(f"target-{builds[-1]}".encode())
        Path(str(path) + ".tbi").write_bytes(f"index-{builds[-1]}".encode())
        return str(path)

    args = (tmp_path / "cache", {"chr1": "chr1"}, [], build, work)
    target = cache.target(*args)
    Path(target).write_bytes(b"corrupted")
    replace = cache.os.replace

    def interrupted(source, destination):
        if str(destination).endswith(".tbi"):
            raise PermissionError("interrupted index publication")
        replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(cache.os, "replace", interrupted)
        assert Path(cache.target(*args)).read_bytes() == b"target-2"
    assert Path(cache.target(*args)).read_bytes() == b"target-3"
    assert builds == [1, 2, 3]


@pytest.mark.parametrize("configured", [False, True])
def test_explicit_fasta_does_not_choose_the_cache_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    from wgsextract_cli.commands import _microarray_chm13 as command
    from wgsextract_cli.core.config import settings

    library = tmp_path / "configured-library"
    monkeypatch.setitem(
        settings, "reference_library", str(library) if configured else None
    )
    seen = []

    def stop_loading(*, cache_dir):
        seen.append(cache_dir)
        raise ValueError("stop after choosing cache")

    monkeypatch.setattr(command, "load_chm13_catalogue", stop_loading)
    with pytest.raises(ValueError, match="choosing cache"):
        command.run_chm13(
            SimpleNamespace(ref_vcf_tab=None, ploidy_file=None),
            SimpleNamespace(root=str(tmp_path / "explicit-fasta-directory")),
            str(tmp_path / "explicit-fasta-directory" / "reference.fa"),
            str(tmp_path / "outputs"),
            "1",
        )
    assert seen == [library / "microarray" / "chm13" if configured else None]


def test_cache_key_tracks_templates_chain_catalogue_and_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_microarray_chm13 import _catalogue

    catalogue = load_chm13_catalogue(_catalogue(tmp_path / "assets"))
    template = tmp_path / "template.tsv"
    template.write_text("rs1\t1\t10\n")
    chain = tmp_path / "hg18ToHg19.over.chain.gz"
    chain.write_bytes(b"chain")
    monkeypatch.setattr(cache, "_resolve_templates_roots", lambda roots: roots)
    monkeypatch.setattr(
        cache, "_template_paths", lambda roots: [("test", False, [str(template)])]
    )
    args = (str(tmp_path / "library"), catalogue, [str(tmp_path)])
    original = cache.cache_directory(*args)
    template.write_text("rs1\t1\t100\n")
    changed = cache.cache_directory(*args)
    assert changed != original
    chain.write_bytes(b"different chain")
    assert cache.cache_directory(*args) != changed
    assert cache.cache_directory(
        args[0], replace(catalogue, cache_key="changed"), args[2]
    ) != cache.cache_directory(*args)
    assert cache.cache_directory(
        str(tmp_path / "other"), catalogue, args[2]
    ) != cache.cache_directory(*args)
    assert cache.cache_directory(None, catalogue, args[2]) is None
