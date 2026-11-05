"""Issue #360: Y haplogroup trees from the registry, config.toml and --tree-file,
run end to end with real bcftools on a synthetic VCF."""

import hashlib
import json
from pathlib import Path

import pytest

from tests.htslib_helpers import bgzip_index, needs_htslib, run_wgsextract
from tests.http_server import serving
from tests.yleaf_helpers import MARKERS
from tests.yleaf_helpers import native_tables as native_tables
from wgsextract_cli.core.config import get_config_path, reload_settings
from wgsextract_cli.core.ref_library import downloads
from wgsextract_cli.core.utils import WGSExtractError

pytestmark = [needs_htslib, pytest.mark.usefixtures("native_tables")]

ROOT = 'ROOT (Y-Chromosome "Adam")'
TREE = {ROOT: ["X"], "X": ["X1", "X2"], "X1": ["X1a"], "X1a": [], "X2": []}
X_MARKERS = (
    "Y\tM-X\tX\t100\tA>G\tA\tG\n"
    "Y\tM-X1\tX1\t110\tA>G\tA\tG\n"
    "Y\tM-X1a\tX1a\t120\tA>G\tA\tG\n"
    "Y\tM-X2\tX2\t150\tA>G\tA\tG\n"
)
# Derived on the synthetic trees' R > R1 > R1b > R1b1 and X > X1 > X1a markers.
GENOTYPES = ((100, 1), (110, 1), (120, 1), (130, 1), (140, 0), (150, 0))


@pytest.fixture
def kit(tmp_path) -> str:
    plain = tmp_path / "kit.vcf"
    plain.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=248956422>\n"
        "##contig=<ID=chrY,length=57227415>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="g">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tkit\n"
        + "".join(
            f"chrY\t{pos}\t.\tA\tG\t50\tPASS\t.\tGT\t{gt}\n" for pos, gt in GENOTYPES
        )
    )
    return bgzip_index(plain)


def _configure(text: str) -> None:
    # [y_trees] tables must follow the config's top-level settings.
    config = get_config_path()
    config.write_text(config.read_text() + text)
    reload_settings()


def _toml(path: Path) -> str:
    return json.dumps(path.as_posix())


def _call(kit: str, outdir: Path, *argv: str) -> tuple[list[str], dict]:
    run_wgsextract(
        "lineage", "y-haplogroup", "--input", kit, "--outdir", str(outdir),
        "--no-ref-inference", *argv,
    )  # fmt: skip
    rows = (outdir / "yleaf/hg_prediction.hg").read_text().splitlines()
    return rows, json.loads((outdir / "yleaf/run.json").read_text())


def _x_tree(folder: Path) -> tuple[Path, Path]:
    folder.mkdir(exist_ok=True)
    (folder / "x.json").write_text(json.dumps(TREE))
    (folder / "x_hg38.txt").write_text(X_MARKERS)
    return folder / "x.json", folder / "x_hg38.txt"


def test_config_tree_is_the_default_and_names_itself(kit, tmp_path, capsys):
    _x_tree(get_config_path().parent / "trees")
    _configure(
        'y_tree = "xtree"\n\n[y_trees.xtree]\nname = "X Tree"\nversion = "2026.1"\n'
        'license = "CC0-1.0"\ntree = "trees/x.json"\n'
        'positions.hg38 = "trees/x_hg38.txt"\n'
    )
    rows, run = _call(kit, tmp_path / "out")
    assert rows[1].split("\t")[1:3] == ["X1a", "M-X1a"]
    assert "tree X Tree 2026.1 (CC0-1.0)" in capsys.readouterr().out
    assert run["tree"]["id"] == "xtree"
    assert run["marker_panel"]["source"] == "custom-tree"
    assert run["marker_panel"]["identifier"] == "x_hg38.txt"


def test_config_copy_of_a_builtin_tree_calls_like_the_builtin(
    kit, tmp_path, native_tables
):
    # The registry must not change a built-in's call; a user copy of its files
    # with the same QC rules is the same tree.
    builtin, _ = _call(kit, tmp_path / "builtin", "--tree", "yfull")
    _configure(
        f'[y_trees.copy]\nversion = "1"\ntree = {_toml(native_tables / "tree.json")}\n'
        f"positions.hg38 = {_toml(native_tables / 'hg38/new_positions.txt')}\n"
        'qc = "yfull"\n'
    )
    copy, _ = _call(kit, tmp_path / "copy", "--tree", "copy")
    assert builtin[1].split("\t")[1] == "R1b1"
    assert copy == builtin


def test_tree_file_with_its_markers(kit, tmp_path, capsys):
    tree, markers = _x_tree(tmp_path / "files")
    rows, run = _call(
        kit, tmp_path / "out", "--tree-file", str(tree), "--pos-file", str(markers)
    )
    assert rows[1].split("\t")[1] == "X1a"
    assert run["tree"]["source"] == str(tree)
    assert "tree x.json" in capsys.readouterr().out
    with pytest.raises(WGSExtractError, match="--tree-file needs --pos-file"):
        _call(kit, tmp_path / "again", "--tree-file", str(tree))
    with pytest.raises(SystemExit):
        _call(kit, tmp_path / "both", "--tree-file", str(tree), "--tree", "isogg")


def test_unknown_tree_lists_the_choices(kit, tmp_path):
    with pytest.raises(WGSExtractError, match="Unknown Y tree 'mine'.*isogg"):
        _call(kit, tmp_path / "out", "--tree", "mine")


def test_config_entry_cannot_shadow_a_builtin_tree(kit, tmp_path):
    _x_tree(get_config_path().parent / "trees")
    _configure(
        '[y_trees.yfull]\nversion = "15"\ntree = "trees/x.json"\n'
        'positions.hg38 = "trees/x_hg38.txt"\n'
    )
    with pytest.raises(WGSExtractError, match="'yfull' is a built-in tree"):
        _call(kit, tmp_path / "out", "--tree", "yfull")


def test_only_the_selected_marker_panel_is_needed(kit, tmp_path):
    # An unreachable ancient panel must not break a normal run, and --ancient
    # must not silently fall back to the normal panel.
    _x_tree(get_config_path().parent / "trees")
    _configure(
        '[y_trees.xtree]\nversion = "1"\ntree = "trees/x.json"\n'
        'positions.hg38 = "trees/x_hg38.txt"\n'
        'positions_ancient.hg19 = {url = "http://127.0.0.1:9/a.txt", '
        f'sha256 = "{"0" * 64}"}}\n'
    )
    rows, run = _call(kit, tmp_path / "normal", "--tree", "xtree")
    assert rows[1].split("\t")[1] == "X1a"
    with pytest.raises(WGSExtractError, match="no hg38 marker table"):
        _call(kit, tmp_path / "ancient", "--tree", "xtree", "--ancient")


def test_missing_tree_file_is_a_file_not_found_error(kit, tmp_path):
    _, markers = _x_tree(tmp_path / "files")
    with pytest.raises(WGSExtractError, match="--tree-file file not found"):
        _call(
            kit, tmp_path / "out", "--tree-file", str(tmp_path / "none.json"),
            "--pos-file", str(markers),
        )  # fmt: skip


@pytest.mark.parametrize(
    ("entry", "tree", "markers", "message"),
    [
        ('positons.hg38 = "m.txt"', TREE, X_MARKERS, "Did you mean 'positions'"),
        ('positions.hg19 = "m.txt"', TREE, X_MARKERS, "no hg38 marker table"),
        (
            'positions.hg38 = {url = "https://example.org/m.txt"}',
            TREE,
            X_MARKERS,
            "a url needs its sha256",
        ),
        ('positions.hg38 = "m.txt"', {"X": []}, X_MARKERS, "starting at"),
        (
            'positions.hg38 = "m.txt"',
            {k: v for k, v in TREE.items() if k != "X2"},
            X_MARKERS,
            r'"X2", a child of "X", has no entry .* "X2": \[\]',
        ),
        (
            'positions.hg38 = "m.txt"',
            {**TREE, "X2": ["X1a"]},
            X_MARKERS,
            'node "X1a" is a child of both',
        ),
        (
            'positions.hg38 = "m.txt"',
            TREE,
            "Y\tM-X\tX\t100\tA>G\tA\n",
            r"m.txt:1: expected 7 tab-separated columns",
        ),
        (
            'positions.hg38 = "m.txt"',
            TREE,
            X_MARKERS + "Y\tM-Z\tZ9\t160\tA>G\tA\tG\n",
            r"1 of 5 markers .* line 5: 'Z9'",
        ),
        (
            'positions.hg38 = "m.txt"',
            TREE,
            "chr1\tM-X\tX\t100\tA>G\tA\tG\n",
            r"m.txt:1: chromosome 'chr1' is not chrY",
        ),
        (
            'positions.hg38 = "m.txt"\nqc = "yfull"',
            TREE,
            X_MARKERS,
            "Use qc = 'none'",
        ),
    ],
)
def test_bad_custom_trees_say_what_to_fix(kit, tmp_path, entry, tree, markers, message):
    folder = get_config_path().parent
    (folder / "t.json").write_text(json.dumps(tree))
    (folder / "m.txt").write_text(markers)
    _configure(f'[y_trees.bad]\nversion = "1"\ntree = "t.json"\n{entry}\n')
    with pytest.raises(WGSExtractError, match=message):
        _call(kit, tmp_path / "out", "--tree", "bad")
    assert not (tmp_path / "out/yleaf/hg_prediction.hg").exists()


def test_remote_tree_files_are_pinned_and_cached(kit, tmp_path, monkeypatch):
    monkeypatch.setattr(
        downloads, "resolve_github_release_asset_sha256", lambda u: None
    )
    monkeypatch.setattr(downloads.time, "sleep", lambda s: None)
    tree = json.dumps(TREE).encode()
    files = {"x.json": tree, "x_hg38.txt": X_MARKERS.encode()}
    library = tmp_path / "reflib"
    with serving(None, files) as server:
        sha = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        signed = server.url_for("x.json") + "?token=SECRET"
        _configure(
            f"reference_library = {_toml(library)}\n\n[y_trees.remote]\n"
            f'version = "1"\ntree = {{url = "{signed}", sha256 = "{sha["x.json"]}"}}\n'
            "positions.hg38 = {url = "
            f'"{server.url_for("x_hg38.txt")}", sha256 = "{sha["x_hg38.txt"]}"}}\n'
        )
        rows, run = _call(kit, tmp_path / "first", "--tree", "remote")
        assert rows[1].split("\t")[1] == "X1a"
        # A signed URL's token stays out of the run's provenance.
        assert run["tree"]["source"] == server.url_for("x.json")
        cached = library / "yleaf/trees/custom" / sha["x.json"] / "x.json"
        assert cached.read_bytes() == tree
        fetched = len(server.gets())
        _call(kit, tmp_path / "second", "--tree", "remote")
        assert len(server.gets()) == fetched

        # A changed upstream file is refused, not silently used.
        server.files["x_hg38.txt"] = MARKERS.encode()
        _configure(
            '\n[y_trees.changed]\nversion = "1"\n'
            f"tree = {_toml(cached)}\npositions.hg38 = {{url = "
            f'"{server.url_for("x_hg38.txt")}", sha256 = "{"0" * 64}"}}\n'
        )
        with pytest.raises(
            WGSExtractError, match="Check the URL and the pinned sha256"
        ):
            _call(kit, tmp_path / "third", "--tree", "changed")
