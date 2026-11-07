"""Issue #503: the yfull tree's CC-BY-4.0 tree and CC-BY-SA YBrowse-derived
marker tables are separate data modules, each pinned and fetched only when a
run needs it. Served from a local HTTP server; real bcftools on a synthetic VCF."""

import hashlib
import io
import json
import logging
import tarfile

import pytest

from tests.htslib_helpers import bgzip_index, needs_htslib, run_wgsextract
from tests.http_server import serving
from tests.yleaf_helpers import MARKERS, make_data
from wgsextract_cli.core.config import get_config_path, reload_settings
from wgsextract_cli.core.ref_library import downloads
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.ydna.yleaf_native import trees

pytestmark = needs_htslib

TREE = {
    'ROOT (Y-Chromosome "Adam")': ["R"],
    "R": ["R1"],
    "R1": ["R1b"],
    "R1b": ["R1b1"],
    "R1b1": [],
}
TREE_FILES = {"tree.json": json.dumps(TREE).encode(), "LICENSE-YTree.md": b"CC-BY"}
POSITIONS_FILES = {
    "NOTICE-YBrowse.md": b"CC-BY-SA",
    **{
        f"{build}/new_positions{kind}.txt": MARKERS.encode()
        for build in trees.BUILDS
        for kind in ("", "_ancient")
    },
}


def _archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as stream:
        for name, payload in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(payload)
            stream.addfile(entry, io.BytesIO(payload))
    return buffer.getvalue()


def _pins(files: dict[str, bytes]) -> dict[str, dict]:
    return {
        name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in files.items()
    }


@pytest.fixture
def served(tmp_path, monkeypatch):
    """The yfull modules on a local server; Yleaf's own data stays synthetic."""
    archives = {
        "tree": ("yfull-tree-1.0.tar.gz", TREE_FILES),
        "positions": ("yfull-ybrowse-positions-1.0.tar.gz", POSITIONS_FILES),
    }
    blobs = {name: _archive(files) for name, files in archives.values()}
    yleaf = make_data(tmp_path / "yleaf-data")
    monkeypatch.setattr(trees, "ensure_data", lambda *a, **k: yleaf)
    monkeypatch.setattr(downloads.time, "sleep", lambda s: None)
    library = tmp_path / "reflib"
    config = get_config_path()
    config.write_text(
        f"reference_library = {json.dumps(library.as_posix())}\n" + config.read_text()
    )
    reload_settings()
    with serving(None, blobs) as server:
        modules = {
            module: {
                "folder": name.removesuffix("-1.0.tar.gz"),
                "license": module,
                "notice": f"Downloading the {module} module (licence {module}).",
                "url": server.url_for(name),
                "sha256": hashlib.sha256(blobs[name]).hexdigest(),
                "files": _pins(files),
            }
            for module, (name, files) in archives.items()
        }
        meta = {**trees.MANIFEST["trees"]["yfull"], "version": "1.0"}
        meta.update(data="modules", modules=modules)
        monkeypatch.setitem(trees.MANIFEST["trees"], "yfull", meta)
        server.library = library  # type: ignore[attr-defined]
        yield server


@pytest.fixture
def kit(tmp_path) -> str:
    plain = tmp_path / "kit.vcf"
    plain.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chrY,length=57227415>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="g">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tkit\n"
        + "".join(
            f"chrY\t{pos}\t.\tA\tG\t50\tPASS\t.\tGT\t1\n"
            for pos in (100, 110, 120, 130)
        )
    )
    return bgzip_index(plain)


def _call(kit, outdir, *argv) -> str:
    run_wgsextract(
        "lineage", "y-haplogroup", "--input", kit, "--outdir", str(outdir),
        "--no-ref-inference", "--build", "hg38", *argv,
    )  # fmt: skip
    rows = (outdir / "yleaf/hg_prediction.hg").read_text().splitlines()
    return rows[1].split("\t")[1]


def _installed(server) -> set[str]:
    modules = server.library / "yleaf/modules"
    if not modules.exists():
        return set()
    return {p.name for p in modules.iterdir() if (p / "1.0").is_dir()}


def test_other_trees_never_fetch_either_yfull_module(served, kit, tmp_path):
    assert _call(kit, tmp_path / "out", "--tree", "isogg") == "R1b1"
    assert not served.paths and _installed(served) == set()


def test_yfull_with_own_markers_fetches_only_the_tree_module(served, kit, tmp_path):
    panel = tmp_path / "panel.txt"
    panel.write_text(MARKERS)
    assert _call(kit, tmp_path / "out", "--pos-file", str(panel)) == "R1b1"
    assert set(served.paths) == {"yfull-tree-1.0.tar.gz"}
    assert _installed(served) == {"yfull-tree"}


def test_yfull_fetches_both_modules_once_with_a_licence_notice(
    served, kit, tmp_path, caplog
):
    caplog.set_level(logging.INFO)
    assert _call(kit, tmp_path / "first") == "R1b1"
    assert _installed(served) == {"yfull-tree", "yfull-ybrowse-positions"}
    tree = served.library / "yleaf/modules/yfull-tree/1.0"
    assert sorted(p.name for p in tree.iterdir()) == sorted(TREE_FILES)
    notices = [r.message for r in caplog.records if "licence" in r.message]
    assert notices == [
        "Downloading the tree module (licence tree).",
        "Downloading the positions module (licence positions).",
    ]
    fetched = len(served.gets())
    caplog.clear()
    assert _call(kit, tmp_path / "second") == "R1b1"
    assert len(served.gets()) == fetched
    assert not [r for r in caplog.records if "licence" in r.message]


def test_each_module_is_verified_against_its_own_pin(served, kit, tmp_path):
    # A tampered positions archive is refused; the tree module is unaffected.
    good = served.files["yfull-ybrowse-positions-1.0.tar.gz"]
    served.files["yfull-ybrowse-positions-1.0.tar.gz"] = _archive(
        {**POSITIONS_FILES, "hg38/new_positions.txt": b"tampered"}
    )
    with pytest.raises(WGSExtractError, match="yfull-ybrowse-positions 1.0"):
        _call(kit, tmp_path / "bad")
    assert _installed(served) == {"yfull-tree"}
    positions = served.library / "yleaf/modules/yfull-ybrowse-positions/1.0"
    assert not positions.exists()

    # A corrupted installed table is replaced from its own module only.
    served.files["yfull-ybrowse-positions-1.0.tar.gz"] = good
    assert _call(kit, tmp_path / "ok") == "R1b1"
    (positions / "hg38/new_positions.txt").write_text("corrupt")
    served.paths.clear()
    assert _call(kit, tmp_path / "repaired") == "R1b1"
    assert set(served.paths) == {"yfull-ybrowse-positions-1.0.tar.gz"}
    assert (positions / "hg38/new_positions.txt").read_text() == MARKERS
