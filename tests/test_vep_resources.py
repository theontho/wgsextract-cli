import gzip
import logging
import os
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools, run_wgsextract
from tests.http_server import serving
from wgsextract_cli import api
from wgsextract_cli.api.vep import VepDownloadParams, VepVerifyParams
from wgsextract_cli.commands._vep_resources import preprocess_vcf_chr_prefix
from wgsextract_cli.core.utils import UsageError, WGSExtractError


def test_preprocess_vcf_chr_prefix_keeps_mito_header_and_records_consistent(tmp_path):
    input_vcf = tmp_path / "input.vcf"
    output_vcf = tmp_path / "output.vcf"
    input_vcf.write_text(
        "##fileformat=VCFv4.2\n"
        "##contig=<ID=1,length=1000>\n"
        "##contig=<ID=MT,length=16569>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "1\t10\trs1\tA\tG\t.\tPASS\t.\n"
        "MT\t20\trsMT\tC\tT\t.\tPASS\t.\n"
        "chrMT\t30\trsChrMT\tG\tA\t.\tPASS\t.\n"
    )

    preprocess_vcf_chr_prefix(str(input_vcf), str(output_vcf))

    assert output_vcf.read_text().splitlines() == [
        "##fileformat=VCFv4.2",
        "##contig=<ID=chr1,length=1000>",
        "##contig=<ID=chrM,length=16569>",
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
        "chr1\t10\trs1\tA\tG\t.\tPASS\t.",
        "chrM\t20\trsMT\tC\tT\t.\tPASS\t.",
        "chrM\t30\trsChrMT\tG\tA\t.\tPASS\t.",
    ]


def test_preprocess_vcf_chr_prefix_reads_gzip_input(tmp_path):
    input_vcf = tmp_path / "input.vcf.gz"
    output_vcf = tmp_path / "output.vcf"
    with gzip.open(input_vcf, "wt") as f:
        f.write(
            "##contig=<ID=M,length=16569>\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "M\t20\trsM\tC\tT\t.\tPASS\t.\n"
        )

    preprocess_vcf_chr_prefix(str(input_vcf), str(output_vcf))

    assert output_vcf.read_text().splitlines() == [
        "##contig=<ID=chrM,length=16569>",
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
        "chrM\t20\trsM\tC\tT\t.\tPASS\t.",
    ]


# --- vep download / verify ------------------------------------------------------
# The cache downloads for real (download_file, curl, tar) from a local HTTP
# server; only the mirror table is pointed at it.

_CACHE_FILE = "homo_sapiens_vep_116_GRCh38.tar.gz"
_CACHE_PATH = f"release-116/variation/indexed_vep_cache/{_CACHE_FILE}"
_CHECKSUMS_PATH = "release-116/variation/indexed_vep_cache/CHECKSUMS"

needs_curl_tar = needs_tools("curl", "tar")


def _cache_tarball(path: Path, chroms=("1", "MT"), version="116_GRCh38") -> bytes:
    """A tar.gz shaped like Ensembl's: homo_sapiens/<version>/{info.txt,<chr>/}."""
    src = path.parent / "tar_src"
    version_dir = src / "homo_sapiens" / version
    version_dir.mkdir(parents=True)
    (version_dir / "info.txt").write_text("species\thomo_sapiens\n")
    for chrom in chroms:
        (version_dir / chrom).mkdir()
        (version_dir / chrom / "1-1000000.gz").write_bytes(b"x")
    with tarfile.open(path, "w:gz") as tar:
        tar.add(src / "homo_sapiens", arcname="homo_sapiens")
    shutil.rmtree(src)
    return path.read_bytes()


@pytest.fixture
def ensembl(tmp_path_factory, monkeypatch):
    """A local Ensembl mirror serving the GRCh38 cache tarball and a CHECKSUMS
    file with its true BSD sum; both mirrors point at it."""
    from wgsextract_cli.commands import _vep_resources as vr

    tarball = tmp_path_factory.mktemp("ensembl") / "served.tar.gz"
    data = _cache_tarball(tarball)
    total, blocks = vr.calculate_bsd_sum(str(tarball))
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    with serving(None) as srv:
        srv.files[_CACHE_PATH] = data
        srv.files[_CHECKSUMS_PATH] = f"{total:05d} {blocks} {_CACHE_FILE}\n".encode()
        for mirror in vr.VEP_MIRRORS:
            monkeypatch.setitem(vr.VEP_MIRRORS, mirror, srv.base)
        yield srv


@needs_curl_tar
def test_download_fetches_verifies_and_extracts_cache(tmp_path, ensembl):
    cache = tmp_path / "vep_cache"

    result = api.vep.download(VepDownloadParams(vep_cache=cache, mirror="ebi"))

    version_dir = cache / "homo_sapiens" / "116_GRCh38"
    assert (version_dir / "info.txt").exists()
    assert sorted(p.name for p in version_dir.iterdir()) == ["1", "MT", "info.txt"]
    assert [o.path for o in result.outputs] == [str(version_dir)]
    # The tarball is gone; the manifest is the only thing beside the cache.
    assert [p.name for p in cache.iterdir()] == ["homo_sapiens"]
    assert sorted(p.name for p in version_dir.parent.iterdir()) == [
        "116_GRCh38",
        "116_GRCh38.manifest.json",
    ]


@needs_curl_tar
def test_download_asks_the_mirror_for_the_release_and_assembly(tmp_path, ensembl):
    result = api.vep.download(
        VepDownloadParams(
            vep_cache=tmp_path, species="mus_musculus", assembly="GRCh37",
            vep_version="110",
        ),
        check=False,
    )  # fmt: skip

    assert result.error is not None and "download failed" in result.error.message
    assert (
        "release-110/variation/indexed_vep_cache/mus_musculus_vep_110_GRCh37.tar.gz"
        in result.error.message
    )


@needs_curl_tar
def test_download_keeps_tarball_it_cannot_extract(tmp_path, ensembl):
    ensembl.files[_CACHE_PATH] = b"not a tarball"
    del ensembl.files[_CHECKSUMS_PATH]

    result = api.vep.download(VepDownloadParams(vep_cache=tmp_path), check=False)

    assert result.error is not None and "Failed to extract" in result.error.message
    assert sorted(p.name for p in tmp_path.iterdir()) == [_CACHE_FILE]


@needs_curl_tar
def test_download_checksum_mismatch_deletes_tarball(tmp_path, ensembl):
    ensembl.files[_CHECKSUMS_PATH] = f"12345 1 {_CACHE_FILE}\n".encode()

    result = api.vep.download(VepDownloadParams(vep_cache=tmp_path), check=False)

    assert result.error is not None
    assert "Checksum verification FAILED" in result.error.message
    assert list(tmp_path.iterdir()) == []


@needs_curl_tar
@pytest.mark.parametrize("checksums", [f"1 1 other_{_CACHE_FILE}\n".encode(), None])
def test_download_without_a_checksum_still_extracts(
    tmp_path, ensembl, checksums, caplog
):
    if checksums is None:
        del ensembl.files[_CHECKSUMS_PATH]
    else:
        ensembl.files[_CHECKSUMS_PATH] = checksums
    cache = tmp_path / "c"

    api.vep.download(VepDownloadParams(vep_cache=cache))

    assert (cache / "homo_sapiens" / "116_GRCh38" / "info.txt").exists()
    assert not list(cache.glob("*.CHECKSUMS"))
    assert "not verified" in caplog.text or "could not be completed" in caplog.text


def _cancel_at(tool: str, token: api.CancelToken):
    def progress(event: api.ProgressEvent) -> None:
        if event.stage == "tool" and f"Running {tool}" in event.message:
            token.cancel()

    return progress


@needs_curl_tar
@pytest.mark.parametrize("tool", ["curl", "tar"])
def test_cancelled_download_removes_the_tarball_and_keeps_the_old_cache(
    tmp_path, ensembl, tool
):
    """Cancelled while checking the checksum or unpacking: no tarball, no
    half-unpacked cache, and the release's old cache untouched."""
    old = _version_dir(tmp_path, ["1", "2"])
    token = api.CancelToken()

    result = api.vep.download(
        VepDownloadParams(vep_cache=tmp_path),
        progress=_cancel_at(tool, token),
        cancel=token,
        check=False,
    )

    assert result.status == "cancelled"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["homo_sapiens"]
    assert sorted(p.name for p in old.iterdir()) == ["1", "2", "info.txt"]


@needs_curl_tar
def test_download_replaces_the_release_old_cache(tmp_path, ensembl):
    old = _version_dir(tmp_path, ["1", "2"])
    other = _version_dir(tmp_path, ["1"], version="115_GRCh38")

    api.vep.download(VepDownloadParams(vep_cache=tmp_path))

    assert sorted(p.name for p in old.iterdir()) == ["1", "MT", "info.txt"]
    assert sorted(p.name for p in other.iterdir()) == ["1", "info.txt"]


@needs_curl_tar
def test_download_expands_a_home_relative_cache(tmp_path, monkeypatch, ensembl):
    monkeypatch.chdir(tmp_path)
    api.vep.download(VepDownloadParams(vep_cache=Path("~/vc")))

    assert (Path.home() / "vc" / "homo_sapiens" / "116_GRCh38").is_dir()
    assert not (tmp_path / "~").exists()


@needs_curl_tar
def test_download_defaults_to_reference_library_then_home(tmp_path, ensembl):
    lib = tmp_path / "lib"
    lib.mkdir()
    run_wgsextract("vep", "download", "--ref", str(lib))
    assert (lib / "vep" / "homo_sapiens" / "116_GRCh38").is_dir()

    run_wgsextract("vep", "download")
    assert (Path.home() / ".vep" / "homo_sapiens" / "116_GRCh38").is_dir()


@needs_curl_tar
def test_download_honours_configured_cache_directory(tmp_path, ensembl):
    from wgsextract_cli.core.config import settings

    configured = tmp_path / "configured"
    settings["vep_cache_directory"] = str(configured)

    run_wgsextract("vep", "download")

    assert (configured / "homo_sapiens" / "116_GRCh38").is_dir()


@needs_curl_tar
def test_download_defaults_to_the_configured_fasta_library(tmp_path, ensembl):
    # PR #498 review: the API used ~/.vep where the CLI used reference_fasta's
    # library.
    from wgsextract_cli.core.config import settings

    genomes = tmp_path / "lib" / "genomes"
    genomes.mkdir(parents=True)
    (genomes / "hg38.fa").write_text(">chr1\nA\n")
    settings["reference_fasta"] = str(genomes / "hg38.fa")

    api.vep.download(VepDownloadParams())

    assert (tmp_path / "lib" / "vep" / "homo_sapiens" / "116_GRCh38").is_dir()
    assert not (Path.home() / ".vep").exists()


class _CancelOnGet(dict):
    """The mirror's files; serving ``path`` cancels ``token``."""

    def __init__(self, files, path, token):
        super().__init__(files)
        self.path, self.token = path, token

    def get(self, key, default=None):
        if key == self.path:
            self.token.cancel()
        return super().get(key, default)


@needs_curl_tar
def test_download_cancelled_while_fetching_leaves_no_tarball(tmp_path, ensembl):
    # PR #498 review: a cancel during the download skipped the cleanup.
    old = _version_dir(tmp_path, ["1", "2"])
    token = api.CancelToken()
    ensembl.files = _CancelOnGet(ensembl.files, _CACHE_PATH, token)

    result = api.vep.download(
        VepDownloadParams(vep_cache=tmp_path), cancel=token, check=False
    )

    assert result.status == "cancelled"
    assert not (tmp_path / _CACHE_FILE).exists()
    assert sorted(p.name for p in old.iterdir()) == ["1", "2", "info.txt"]


@needs_curl_tar
@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs a folder whose entries can't be moved",
)
def test_download_that_fails_partway_puts_the_old_cache_back(tmp_path, ensembl):
    # PR #498 review: the old release was deleted before the new one was in
    # place, so a failure part-way lost it.
    served = tmp_path / "served.tar.gz"
    _cache_tarball(served)
    src = tmp_path / "src"
    with tarfile.open(served) as tar:
        tar.extractall(src, filter="data")
    shutil.copytree(src / "homo_sapiens", src / "zz_species")
    with tarfile.open(served, "w:gz") as tar:
        for top in ("homo_sapiens", "zz_species"):
            tar.add(src / top, arcname=top)
    ensembl.files[_CACHE_PATH] = served.read_bytes()
    del ensembl.files[_CHECKSUMS_PATH]
    cache = tmp_path / "cache"
    old = _version_dir(cache, ["1", "2"])
    locked = _version_dir(cache, ["1"], species="zz_species").parent
    locked.chmod(0o555)  # its old release can't be moved aside
    try:
        result = api.vep.download(VepDownloadParams(vep_cache=cache), check=False)
    finally:
        locked.chmod(0o755)

    assert result.status == "failed"
    assert sorted(p.name for p in old.iterdir()) == ["1", "2", "info.txt"]
    assert sorted(p.name for p in cache.iterdir()) == sorted(
        [_CACHE_FILE, "homo_sapiens", "zz_species"]
    )


@needs_curl_tar
def test_a_server_download_refuses_a_cache_link_leading_elsewhere(tmp_path, ensembl):
    # PR #498 review: the guard checks the cache folder, not a link in it.
    from wgsextract_cli.core import read_scope
    from wgsextract_cli.server.operations import PathGuard

    cache, outside = tmp_path / "cache", tmp_path / "outside"
    cache.mkdir()
    outside.mkdir()
    try:
        (cache / "homo_sapiens").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links need privileges here")

    with read_scope.limited_to(PathGuard([cache]).allows):
        result = api.vep.download(VepDownloadParams(vep_cache=cache), check=False)

    assert result.error is not None and "leads outside" in result.error.message
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "field", [{"species": "../../evil"}, {"vep_version": "116/../../x"}]
)
def test_a_species_or_release_cannot_leave_the_cache(tmp_path, field):
    with pytest.raises(UsageError, match="Invalid"):
        VepDownloadParams(vep_cache=tmp_path, **field)
    with pytest.raises(UsageError, match="Invalid"):
        VepVerifyParams(vep_cache=tmp_path, **field)


def _version_dir(
    cache: Path,
    chroms,
    info: bool = True,
    version: str = "116_GRCh38",
    species: str = "homo_sapiens",
) -> Path:
    version_dir = cache / species / version
    version_dir.mkdir(parents=True)
    if info:
        (version_dir / "info.txt").write_text("")
    for chrom in chroms:
        (version_dir / chrom).mkdir()
    return version_dir


def test_verify_complete_cache(tmp_path, caplog):
    chroms = [str(c) for c in range(1, 23)] + ["X", "Y", "MT"]
    _version_dir(tmp_path, chroms)
    caplog.set_level(logging.INFO)

    run_wgsextract("vep", "verify", "--vep-cache", str(tmp_path))

    assert "Found info.txt" in caplog.text
    assert "All primary chromosomal directories" in caplog.text
    assert "Missing" not in caplog.text


def test_verify_warns_about_missing_chromosomes(tmp_path):
    _version_dir(tmp_path, [str(c) for c in range(1, 22)] + ["X"])

    result = api.vep.verify(VepVerifyParams(vep_cache=tmp_path))

    assert result.metrics["missing_chromosomes"] == 3
    assert [(w.code, w.details) for w in result.warnings] == [
        ("chromosomes_missing", {"chromosomes": "22,Y,MT"})
    ]


def test_verify_reads_the_reference_library_cache(tmp_path):
    lib = tmp_path / "lib"
    (lib / "genomes").mkdir(parents=True)
    _version_dir(lib / "vep", ["1"])

    result = api.vep.verify(VepVerifyParams(ref=lib))

    assert result.metrics["missing_chromosomes"] == 24


@pytest.mark.parametrize(("chroms", "info"), [(["1"], False), ([], True)])
def test_verify_fails_an_unusable_cache(tmp_path, chroms, info):
    _version_dir(tmp_path, chroms, info=info)
    with pytest.raises(WGSExtractError, match="incomplete; download it again"):
        run_wgsextract("vep", "verify", "--vep-cache", str(tmp_path))


def test_verify_missing_version_fails(tmp_path):
    _version_dir(tmp_path, ["1"])
    with pytest.raises(WGSExtractError, match=r"not found: .*115_GRCh37"):
        run_wgsextract(
            "vep", "verify", "--vep-cache", str(tmp_path),
            "--vep-version", "115", "--assembly", "GRCh37",
        )  # fmt: skip
