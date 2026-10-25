"""Genome download-and-process, deletion, bootstrap, ploidy and gnomAD installs,
with download_file faked by writing small real files to its destination."""

import contextlib
import gzip
import io
import logging
import random
import struct
import subprocess
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_htslib, needs_tools
from wgsextract_cli.commands.ref import library_commands
from wgsextract_cli.core import reference_processing as rp
from wgsextract_cli.core.bgzf import ensure_bgzf, is_bgzf
from wgsextract_cli.core.constants import BOOTSTRAP_FILENAME
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.ref_library.catalog import get_genome_status
from wgsextract_cli.core.utils import WGSExtractError, run_command

_rng = random.Random(11)
CHR_A = "N" * 60 + "".join(_rng.choice("ACGT") for _ in range(3000)) + "N" * 40
CHR_B = "".join(_rng.choice("ACGT") for _ in range(2000))
FASTA = f">chrA\n{CHR_A}\n>chrB\n{CHR_B}\n".encode()


def _genome(final="hs38.fa.gz", url="https://example.org/hs38.fa.gz"):
    return {"final": final, "url": url, "label": final, "source": "test"}


def _bgzip(data: bytes) -> bytes:
    bgzip = [get_tool_path("bgzip"), "-c"]
    return subprocess.run(bgzip, input=data, capture_output=True, check=True).stdout


def _compress(kind: str, data: bytes) -> bytes:
    return _bgzip(data) if kind == "bgzf" else gzip.compress(data)


def _fake_download(monkeypatch, payloads, calls=None):
    """Make download_file write payloads[url] (bytes, or None to fail) to dest."""

    def download(url, dest, progress_callback=None, cancel_event=None):
        if calls is not None:
            calls.append((url, Path(dest + ".partial").exists()))
        data = payloads[url]
        if data is None:
            return False
        Path(dest).write_bytes(data)
        Path(dest + ".partial").unlink(missing_ok=True)
        return True

    monkeypatch.setattr(rp, "download_file", download)


def _region(fasta: Path, region: str) -> str:
    out = run_command(
        [get_tool_path("samtools"), "faidx", str(fasta), region], capture_output=True
    ).stdout
    return "".join(out.splitlines()[1:])


def _assert_installed(lib: Path, final: str) -> Path:
    path = lib / "genomes" / final
    assert get_genome_status(final, str(lib)) == "installed"
    assert is_bgzf(str(path))
    assert Path(f"{path}.gzi").is_file()
    dict_lines = Path(rp.reference_dict_path(path)).read_text().splitlines()
    assert [line.split("\t")[1] for line in dict_lines[1:]] == ["SN:chrA", "SN:chrB"]
    assert _region(path, "chrA:61-80") == CHR_A[60:80]
    assert _region(path, "chrB:1991-2000") == CHR_B[-10:]
    leftovers = {p.name for p in path.parent.iterdir()} - {
        path.name,
        f"{path.name}.fai",
        f"{path.name}.gzi",
        Path(rp.reference_dict_path(path)).name,
    }
    assert not leftovers
    return path


# --- download_and_process_genome ---------------------------------------------


@needs_htslib
@pytest.mark.parametrize("kind", ["gzip", "bgzf"])
def test_download_installs_an_indexed_bgzf_genome(tmp_path, monkeypatch, kind):
    payload = _compress(kind, FASTA)
    _fake_download(monkeypatch, {_genome()["url"]: payload})
    statuses: list[str] = []
    assert rp.download_and_process_genome(
        _genome(), str(tmp_path), status_callback=statuses.append
    )
    path = _assert_installed(tmp_path, "hs38.fa.gz")
    if kind == "bgzf":
        assert path.read_bytes() == payload  # already BGZF: not recompressed
        assert "Processing: Recompressing (BGZF)..." not in statuses
    else:
        assert statuses[0] == "Processing: Recompressing (BGZF)..."
    assert statuses[-1] == "Processing: Indexing FASTA..."


@needs_htslib
def test_download_installs_a_single_file_zip_served_as_the_genome(
    tmp_path, monkeypatch
):
    # The YSEQ hg19 mirror serves hg19.zip under the final name hg19_yseq.fa.gz.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("hg19.fa", FASTA)
    genome = _genome("hg19_yseq.fa.gz", "https://example.org/hg19.zip")
    _fake_download(monkeypatch, {genome["url"]: buf.getvalue()})
    assert rp.download_and_process_genome(genome, str(tmp_path))
    _assert_installed(tmp_path, "hg19_yseq.fa.gz")


@needs_htslib
def test_download_installs_an_uncompressed_fasta_served_under_a_gz_name(
    tmp_path, monkeypatch
):
    genome = _genome("hs38DH.fa.gz", "https://example.org/hs38DH.fa")
    _fake_download(monkeypatch, {genome["url"]: FASTA})
    assert rp.download_and_process_genome(genome, str(tmp_path))
    _assert_installed(tmp_path, "hs38DH.fa.gz")


@needs_htslib
def test_installed_genome_is_reindexed_without_downloading(tmp_path, monkeypatch):
    calls: list = []
    _fake_download(monkeypatch, {_genome()["url"]: gzip.compress(FASTA)}, calls)
    assert rp.download_and_process_genome(_genome(), str(tmp_path))
    path = tmp_path / "genomes" / "hs38.fa.gz"
    Path(rp.reference_dict_path(path)).unlink()

    assert rp.download_and_process_genome(_genome(), str(tmp_path))
    assert len(calls) == 1
    _assert_installed(tmp_path, "hs38.fa.gz")


@needs_htslib
def test_partial_is_resumed_unless_restarting(tmp_path, monkeypatch):
    genomes = tmp_path / "genomes"
    genomes.mkdir()
    partial = genomes / "hs38.fa.gz.partial"
    calls: list = []
    _fake_download(monkeypatch, {_genome()["url"]: _bgzip(FASTA)}, calls)

    partial.write_bytes(b"first half")
    assert get_genome_status("hs38.fa.gz", str(tmp_path)) == "incomplete"
    assert rp.download_and_process_genome(_genome(), str(tmp_path))
    assert calls == [(_genome()["url"], True)]  # the partial was left to resume

    calls.clear()
    partial.write_bytes(b"stale")
    assert rp.download_and_process_genome(_genome(), str(tmp_path), restart=True)
    assert calls == [(_genome()["url"], False)]  # installed copy and partial dropped
    _assert_installed(tmp_path, "hs38.fa.gz")


@needs_htslib
def test_failed_download_installs_nothing(tmp_path, monkeypatch):
    _fake_download(monkeypatch, {_genome()["url"]: None})
    assert not rp.download_and_process_genome(_genome(), str(tmp_path))
    assert get_genome_status("hs38.fa.gz", str(tmp_path)) == "missing"


@needs_htslib
@pytest.mark.parametrize("kind", ["gzip", "bgzf"])
def test_cancelled_install_is_not_reported_installed(tmp_path, monkeypatch, kind):
    _fake_download(monkeypatch, {_genome()["url"]: _compress(kind, FASTA)})
    cancel = threading.Event()
    cancel.set()
    assert not rp.download_and_process_genome(
        _genome(), str(tmp_path), cancel_event=cancel
    )
    assert get_genome_status("hs38.fa.gz", str(tmp_path)) == "unindexed"
    assert not list((tmp_path / "genomes").glob("*.tmp.gz"))


@needs_htslib
def test_failed_reinstall_does_not_read_as_installed(tmp_path, monkeypatch):
    payload = {_genome()["url"]: gzip.compress(FASTA)}
    _fake_download(monkeypatch, payload)
    assert rp.download_and_process_genome(_genome(), str(tmp_path))

    whole = gzip.compress(FASTA)
    payload[_genome()["url"]] = whole[: len(whole) // 2]  # truncated gzip
    with contextlib.suppress(WGSExtractError):
        rp.download_and_process_genome(_genome(), str(tmp_path), restart=True)
    assert get_genome_status("hs38.fa.gz", str(tmp_path)) != "installed"


@needs_htslib
def test_corrupt_download_falls_through_to_the_next_mirror(tmp_path, monkeypatch):
    whole = gzip.compress(FASTA)
    first = _genome(url="https://example.org/a/hs38.fa.gz")
    second = _genome(url="https://example.org/b/hs38.fa.gz")
    _fake_download(
        monkeypatch, {first["url"]: whole[: len(whole) // 2], second["url"]: whole}
    )
    library_commands._install_from_mirrors([first, second], str(tmp_path))
    _assert_installed(tmp_path, "hs38.fa.gz")


# --- process_reference_file ---------------------------------------------------


@needs_htslib
def test_plain_fasta_is_compressed_beside_and_replaced(tmp_path):
    fasta = tmp_path / "pet.fna"
    fasta.write_bytes(FASTA)
    assert rp.process_reference_file(str(fasta))
    assert not fasta.exists()
    bgzf = tmp_path / "pet.fna.gz"
    assert is_bgzf(str(bgzf))
    assert Path(f"{bgzf}.fai").is_file() and (tmp_path / "pet.dict").is_file()
    assert _region(bgzf, "chrB:1-10") == CHR_B[:10]


@needs_htslib
def test_index_failure_returns_false_with_samtools_error(tmp_path, caplog):
    bad = tmp_path / "bad.fa.gz"
    bad.write_bytes(_bgzip(b"not a fasta\n"))
    with caplog.at_level(logging.ERROR):
        assert not rp.process_reference_file(str(bad))
    assert "samtools faidx failed with exit code" in caplog.text
    assert not Path(f"{bad}.fai").exists()


@needs_tools("sleep")
def test_reference_tool_cancel_stops_the_process(tmp_path):
    cancel = threading.Event()
    cancel.set()
    assert rp._run_reference_tool(["sleep", "30"], cancel) is False
    with pytest.raises(WGSExtractError, match="false failed with exit code 1"):
        rp._run_reference_tool(["false"], None)


# --- delete_genome / has_ref_ns ----------------------------------------------


def _touch(*paths: Path) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")


def test_delete_genome_removes_every_sidecar_and_nothing_else(tmp_path):
    g = tmp_path / "genomes"
    ours = [
        g / name
        for name in (
            "hs38.fa.gz",
            "hs38.fa.gz.partial",
            "hs38.fa.gz.fai",
            "hs38.fa.gz.gzi",
            "hs38.fa.gz.dict",
            "hs38.dict",
            "hs38_ncnt.csv",
            "hs38_nbin.csv",
            "hs38.wgse",
        )
    ]
    others = [g / "hs38d1.fna.gz", g / "hs38d1.dict", g / "hs38d1_ncnt.csv"]
    _touch(*ours, *others)
    assert rp.has_ref_ns("hs38.fa.gz", str(tmp_path))

    assert rp.delete_genome("hs38.fa.gz", str(tmp_path))
    assert sorted(p.name for p in g.iterdir()) == sorted(p.name for p in others)
    assert get_genome_status("hs38.fa.gz", str(tmp_path)) == "missing"
    assert not rp.has_ref_ns("hs38.fa.gz", str(tmp_path))
    assert rp.delete_genome("hs38.fa.gz", str(tmp_path))  # nothing left: still fine


def test_delete_genome_removes_download_state_and_aligner_indexes(tmp_path):
    g = tmp_path / "genomes"
    suffixes = [".partial.segments", ".bwt", ".sa", ".0123", ".bwt.2bit.64", ".alt"]
    _touch(*(g / f"hs38.fa.gz{s}" for s in suffixes))
    rp.delete_genome("hs38.fa.gz", str(tmp_path))
    assert not list(g.iterdir())


@pytest.mark.parametrize("sidecar", ["hs38_ncnt.csv", "hs38_nbin.csv"])
def test_has_ref_ns_finds_either_n_count_file(tmp_path, sidecar):
    assert not rp.has_ref_ns("hs38.fa.gz", "")
    assert not rp.has_ref_ns("hs38.fa.gz", str(tmp_path))
    _touch(tmp_path / "genomes" / sidecar)
    assert rp.has_ref_ns("hs38.fa.gz", str(tmp_path))


# --- bootstrap ----------------------------------------------------------------


def _tarball(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def bootstrap_download(monkeypatch):
    """Fake the bootstrap download: each attempt pops the next payload."""
    attempts: list[bytes | None] = []

    def download(url, dest, progress_callback=None, cancel_event=None):
        assert Path(dest).name == BOOTSTRAP_FILENAME
        data = attempts.pop(0)
        if data is None:
            return False
        Path(dest).write_bytes(data)
        return True

    monkeypatch.setattr(rp, "download_file", download)
    return attempts


@needs_tools("bcftools")
def test_bootstrap_downloads_extracts_and_flattens(tmp_path, bootstrap_download):
    lib = tmp_path / "lib"
    (lib / "ref").mkdir(parents=True)
    (lib / "ref" / "notes.txt").write_text("mine")
    bootstrap_download += [
        None,
        _tarball(
            {
                "reference/ref/All_SNPs_hg38_ref.tab.gz": b"snps",
                "ref/notes.txt": b"theirs",
                "reference/._All_SNPs_hg38_ref.tab.gz": b"appledouble",
                "__MACOSX/reference/ref/._x": b"metadata",
            }
        ),
    ]
    assert rp.download_bootstrap(str(lib))
    assert not bootstrap_download  # retried once after the failed attempt
    assert (lib / "ref" / "All_SNPs_hg38_ref.tab.gz").read_bytes() == b"snps"
    assert (lib / "ref" / "notes.txt").read_text() == "mine"
    assert not (lib / BOOTSTRAP_FILENAME).exists()
    assert not (lib / "reference").exists() and not (lib / "__MACOSX").exists()
    assert (lib / "ploidy_hg19.txt").is_file() and (lib / "ploidy_hg38.txt").is_file()

    # Installed now: a second run downloads nothing.
    assert rp.download_bootstrap(str(lib))


def test_bootstrap_gives_up_after_three_failed_downloads(tmp_path, bootstrap_download):
    bootstrap_download += [None, None, None]
    assert not rp.download_bootstrap(str(tmp_path / "lib"))
    assert not bootstrap_download
    assert not (tmp_path / "lib" / "ploidy_hg38.txt").exists()


def test_corrupt_bootstrap_archive_fails(tmp_path, bootstrap_download):
    bootstrap_download.append(b"<html>not a tarball</html>")
    assert not rp.download_bootstrap(str(tmp_path))
    assert not rp.bootstrap_has_support_assets(str(tmp_path))


def test_flattening_keeps_conflicting_duplicates_in_place(tmp_path, caplog):
    nested = tmp_path / "reference" / "ref"
    nested.mkdir(parents=True)
    (tmp_path / "ref").mkdir()
    for name, ours, theirs in [("same.txt", "a", "a"), ("diff.txt", "a", "b")]:
        (tmp_path / "ref" / name).write_text(ours)
        (nested / name).write_text(theirs)
    (tmp_path / "ref" / "sub").mkdir()
    (nested / "sub").write_text("a file where a directory is")
    with caplog.at_level(logging.WARNING):
        rp.normalize_bootstrap_layout(str(tmp_path))
    assert sorted(p.name for p in nested.iterdir()) == ["diff.txt", "sub"]
    assert (tmp_path / "ref" / "diff.txt").read_text() == "a"
    assert "Leaving duplicate bootstrap file in place" in caplog.text


def test_bootstrap_assets_are_recognized_anywhere_in_the_library(tmp_path):
    assert not rp.bootstrap_has_support_assets(str(tmp_path / "missing"))
    assert not rp.bootstrap_has_support_assets(str(tmp_path))
    for name in ["snps_hg38.vcf.gz", "x_ref.tab.gz", "All_SNPs_GRCh38.tab.gz"]:
        path = tmp_path / "deep" / name
        _touch(path)
        assert rp.bootstrap_has_support_assets(str(tmp_path)), name
        path.unlink()


# --- ploidy files -------------------------------------------------------------


def _ploidy_rules(path: Path) -> list[list[str]]:
    lines = path.read_text().splitlines()
    return [line.split() for line in lines if line.strip() and line[0] != "#"]


@needs_tools("bcftools")
def test_ploidy_files_make_male_x_haploid_and_keep_existing(tmp_path):
    (tmp_path / "ploidy_hg19.txt").write_text("custom\n")
    assert rp.install_ploidy_files(str(tmp_path))
    assert (tmp_path / "ploidy_hg19.txt").read_text() == "custom\n"
    rules = _ploidy_rules(tmp_path / "ploidy_hg38.txt")
    assert ["X", "2781480"] == rules[1][:2] and rules[1][3:] == ["M", "1"]
    assert ["*", "*", "*", "M", "2"] in rules


def test_ploidy_falls_back_to_templates_without_bcftools(tmp_path, monkeypatch):
    def no_bcftools(*args, **kwargs):
        raise FileNotFoundError("bcftools")

    monkeypatch.setattr(rp, "run_command", no_bcftools)
    assert rp.install_ploidy_files(str(tmp_path))
    assert (tmp_path / "ploidy_hg19.txt").read_text() == rp._PLOIDY_TEMPLATES["GRCh37"]


def test_ploidy_write_failure_is_reported(tmp_path):
    (tmp_path / "ploidy_hg38.txt").mkdir()
    assert not rp.install_ploidy_files(str(tmp_path))
    assert (tmp_path / "ploidy_hg19.txt").is_file()


@needs_tools("bcftools")
def test_ploidy_files_cover_chr_named_references(tmp_path):
    assert rp.install_ploidy_files(str(tmp_path))
    for build in ("hg19", "hg38"):
        chroms = {r[0] for r in _ploidy_rules(tmp_path / f"ploidy_{build}.txt")}
        assert {"chrX", "chrY", "chrM"} <= chroms


# --- gnomAD -------------------------------------------------------------------

_VCF = (
    b"##fileformat=VCFv4.2\n##contig=<ID=chr1,length=1000>\n"
    b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nchr1\t100\t.\tA\tG\t.\t.\t.\n"
)


@needs_htslib
def test_gnomad_downloads_are_indexed_and_failures_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(
        rp, "GNOMAD_URLS", {"hg38": "https://x/38.bgz", "hg19": "https://x/19.bgz"}
    )
    _fake_download(
        monkeypatch, {"https://x/38.bgz": _bgzip(_VCF), "https://x/19.bgz": None}
    )
    assert not rp.download_gnomad(str(tmp_path))
    assert (tmp_path / "ref" / "gnomad_hg38.vcf.bgz.tbi").is_file()
    assert not (tmp_path / "ref" / "gnomad_hg19.vcf.bgz").exists()

    _fake_download(monkeypatch, {"https://x/38.bgz": _VCF, "https://x/19.bgz": None})
    (tmp_path / "ref" / "gnomad_hg38.vcf.bgz.tbi").unlink()
    assert not rp.download_gnomad(str(tmp_path))  # plain VCF: tabix refuses it
    assert not (tmp_path / "ref" / "gnomad_hg38.vcf.bgz.tbi").exists()

    cancel = threading.Event()
    cancel.set()
    assert not rp.download_gnomad(str(tmp_path), cancel_event=cancel)


@pytest.mark.parametrize("alias, name", [("GRCh37", "hg19"), ("GRCh38", "hg38")])
def test_old_template_ploidy_file_is_replaced(tmp_path, alias, name):
    old = tmp_path / f"ploidy_{name}.txt"
    old.write_text(rp._OLD_PLOIDY_TEMPLATES[alias])
    assert rp.install_ploidy_files(str(tmp_path))
    assert "chrX" in old.read_text()


@needs_tools("bcftools")
@pytest.mark.parametrize("alias", ["GRCh37", "GRCh38"])
def test_template_matches_bcftools_definition(alias):
    """The fallback for when bcftools can't be asked is bcftools' own table."""

    def rules(text):
        return [line.split() for line in text.splitlines() if line.strip()]

    assert rules(rp._ploidy_content(alias)) == rules(rp._PLOIDY_TEMPLATES[alias])


@needs_htslib
def test_ensure_bgzf_refuses_a_zip_of_several_files(tmp_path):
    path = tmp_path / "ref.fa.gz"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("a.fa", FASTA)
        z.writestr("b.fa", FASTA)
    with pytest.raises(WGSExtractError, match="zip of 2 files"):
        ensure_bgzf(str(path))
    assert not (tmp_path / "ref.fa.gz.tmp.gz").exists()


@needs_htslib
@pytest.mark.parametrize("encrypted", [True, False])
@pytest.mark.parametrize("filename", ["ref.zip", "ref.fa.gz"])
def test_bgzf_decoder_failure_preserves_the_archive(tmp_path, encrypted, filename):
    path = tmp_path / filename
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ref.fa", FASTA)
    payload = bytearray(path.read_bytes())
    central = payload.index(b"PK\x01\x02")
    # The standard library cannot write encrypted/unsupported ZIP members.
    # Set their header fields to exercise the real decoder's failure paths.
    if encrypted:
        struct.pack_into("<H", payload, 6, 1)
        struct.pack_into("<H", payload, central + 8, 1)
    else:
        struct.pack_into("<H", payload, 8, 99)
        struct.pack_into("<H", payload, central + 10, 99)
    path.write_bytes(payload)

    with pytest.raises(WGSExtractError, match="Could not decompress"):
        ensure_bgzf(str(path))

    assert path.read_bytes() == payload
    assert not Path(str(path) + ".tmp.gz").exists()
    if not filename.endswith(".gz"):
        assert not Path(str(path) + ".gz").exists()
