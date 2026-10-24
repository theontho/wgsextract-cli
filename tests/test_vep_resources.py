import argparse
import gzip
import logging
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.htslib_helpers import run_wgsextract
from wgsextract_cli.commands._vep_resources import preprocess_vcf_chr_prefix
from wgsextract_cli.core.utils import WGSExtractError


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

_CACHE_FILE = "homo_sapiens_vep_116_GRCh38.tar.gz"
_EBI_BASE = (
    "https://ftp.ebi.ac.uk/ensemblorg/pub/release-116/variation/indexed_vep_cache"
)


def _cache_tarball(path: Path, chroms=("1", "MT")) -> bytes:
    """A tar.gz shaped like Ensembl's: homo_sapiens/116_GRCh38/{info.txt,<chr>/}."""
    src = path.parent / "tar_src"
    version_dir = src / "homo_sapiens" / "116_GRCh38"
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
def fake_ensembl(tmp_path, monkeypatch):
    """Serve the cache tarball and its CHECKSUMS line without the network.

    Set ``.checksums`` to the CHECKSUMS text (default: the tarball's true BSD sum)
    or to None to make the curl fetch fail."""
    from wgsextract_cli.commands import _vep_resources as vr

    data = _cache_tarball(tmp_path / "served.tar.gz")
    true_sum = vr.calculate_bsd_sum(str(tmp_path / "served.tar.gz"))
    state = SimpleNamespace(
        data=data,
        checksums=f"{true_sum[0]:05d} {true_sum[1]} {_CACHE_FILE}\n",
        urls=[],
        cancel_after=None,
    )
    real_run_command = vr.run_command

    def fake_download(url, dest, progress_callback=None, cancel_event=None):
        state.urls.append(url)
        Path(dest).write_bytes(state.data)
        if state.cancel_after is not None:
            state.cancel_after.set()
        return True

    def fake_run_command(cmd, *args, **kwargs):
        if cmd[0] != "curl":
            return real_run_command(cmd, *args, **kwargs)
        state.urls.append(cmd[-1])
        if state.checksums is None:
            raise subprocess.CalledProcessError(22, cmd, "", "404")
        Path(cmd[cmd.index("-o") + 1]).write_text(state.checksums)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(vr, "download_file", fake_download)
    monkeypatch.setattr(vr, "run_command", fake_run_command)
    monkeypatch.setattr(vr, "verify_dependencies", lambda tools: None)
    return state


def test_download_fetches_verifies_and_extracts_cache(tmp_path, fake_ensembl):
    cache = tmp_path / "vep_cache"

    run_wgsextract("vep", "download", "--mirror", "ebi", "--vep-cache", str(cache))

    assert fake_ensembl.urls == [f"{_EBI_BASE}/{_CACHE_FILE}", f"{_EBI_BASE}/CHECKSUMS"]
    assert (cache / "homo_sapiens" / "116_GRCh38" / "info.txt").exists()
    assert sorted(p.name for p in cache.iterdir()) == ["homo_sapiens"]


def test_download_keeps_tarball_it_cannot_extract(tmp_path, fake_ensembl):
    fake_ensembl.checksums = ""
    fake_ensembl.data = b"not a tarball"
    with pytest.raises(WGSExtractError, match="Failed to extract"):
        run_wgsextract(
            "vep", "download", "--species", "mus_musculus", "--assembly", "GRCh37",
            "--vep-version", "110", "--vep-cache", str(tmp_path),
        )  # fmt: skip
    assert fake_ensembl.urls[0] == (
        "https://ftp.ensembl.org/pub/release-110/variation/indexed_vep_cache/"
        "mus_musculus_vep_110_GRCh37.tar.gz"
    )
    assert (tmp_path / "mus_musculus_vep_110_GRCh37.tar.gz").exists()


def test_download_checksum_mismatch_deletes_tarball(tmp_path, fake_ensembl):
    fake_ensembl.checksums = f"12345 1 {_CACHE_FILE}\n"

    with pytest.raises(WGSExtractError, match="Checksum verification FAILED"):
        run_wgsextract("vep", "download", "--vep-cache", str(tmp_path))

    assert sorted(p.name for p in tmp_path.iterdir()) == ["served.tar.gz"]


@pytest.mark.parametrize("checksums", [f"1 1 other_{_CACHE_FILE}\n", None])
def test_download_without_a_checksum_still_extracts(
    tmp_path, fake_ensembl, checksums, caplog
):
    fake_ensembl.checksums = checksums
    cache = tmp_path / "c"

    run_wgsextract("vep", "download", "--vep-cache", str(cache))

    assert (cache / "homo_sapiens" / "116_GRCh38" / "info.txt").exists()
    assert not list(cache.glob("*.CHECKSUMS"))
    assert "not verified" in caplog.text or "could not be completed" in caplog.text


def test_download_cancelled_after_fetch_stops_before_extraction(tmp_path, fake_ensembl):
    from wgsextract_cli.commands import _vep_resources as vr

    cancel = threading.Event()
    fake_ensembl.cancel_after = cancel
    args = argparse.Namespace(
        species="homo_sapiens", assembly="GRCh38", vep_version="116",
        mirror="uk", vep_cache=str(tmp_path / "c"), cancel_event=cancel,
    )  # fmt: skip

    with pytest.raises(WGSExtractError, match="cancelled before extraction"):
        vr.cmd_vep_download(args)
    assert not (tmp_path / "c" / "homo_sapiens").exists()


def test_download_reports_cancellation(tmp_path, monkeypatch):
    from wgsextract_cli.commands import _vep_resources as vr

    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(vr, "verify_dependencies", lambda tools: None)
    monkeypatch.setattr(vr, "download_file", lambda *a: False)
    args = argparse.Namespace(
        species="homo_sapiens", assembly="GRCh38", vep_version="116",
        mirror="uk", vep_cache=str(tmp_path), cancel_event=cancel,
    )  # fmt: skip
    with pytest.raises(WGSExtractError, match="download cancelled"):
        vr.cmd_vep_download(args)


def test_download_defaults_to_reference_library_then_home(tmp_path, fake_ensembl):
    lib = tmp_path / "lib"
    lib.mkdir()
    run_wgsextract("vep", "download", "--ref", str(lib))
    assert (lib / "vep" / "homo_sapiens" / "116_GRCh38").is_dir()

    run_wgsextract("vep", "download")
    assert (Path.home() / ".vep" / "homo_sapiens" / "116_GRCh38").is_dir()


def test_download_honours_configured_cache_directory(tmp_path, fake_ensembl):
    from wgsextract_cli.core.config import settings

    configured = tmp_path / "configured"
    settings["vep_cache_directory"] = str(configured)

    run_wgsextract("vep", "download")

    assert (configured / "homo_sapiens" / "116_GRCh38").is_dir()


def _version_dir(cache: Path, chroms, info: bool = True) -> Path:
    version_dir = cache / "homo_sapiens" / "116_GRCh38"
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


def test_verify_warns_about_missing_chromosomes(tmp_path, caplog):
    _version_dir(tmp_path, [str(c) for c in range(1, 22)] + ["X"])

    run_wgsextract("vep", "verify", "--vep-cache", str(tmp_path))

    assert "Missing chromosomal data for: 22, Y, MT" in caplog.text


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
