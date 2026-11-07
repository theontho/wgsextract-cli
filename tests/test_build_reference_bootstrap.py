"""scripts/build_reference_bootstrap.py drops the YBrowse data (#509)."""

import hashlib
import io
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_reference_bootstrap.py"
README = (
    "Liftover chains:\n-- hg19-chm13v2.chain\n\n"
    "Y Chromosome SNPs file (yBrowse.org):\n-- snps_hg38.vcf.gz (synthetic)\n\n"
    "Exome BED:\n-- exome.bed\n"
).replace("\n", "\r\n")
MEMBERS = {
    "reference/00README.txt": README.encode(),
    "reference/hg19-chm13v2.chain": b"chain",
    "reference/snps_hg38.vcf.gz": b"synthetic",
    "reference/snps_hg38.vcf.gz.tbi": b"synthetic",
    "reference/snps_grch37.vcf.gz.tbi.orig": b"synthetic",
    "reference/._hg19-chm13v2.chain": b"appledouble",
    "reference/.DS_Store": b"finder",
}


def _source(path: Path) -> str:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in MEMBERS.items():
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), 1_600_000_000
            tar.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build(source: Path, sha: str, output: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(source)]
        + ["--source-sha256", sha, "--version", "9.9.9", "--output", str(output)],
        capture_output=True,
        text=True,
    )


def test_bootstrap_build_drops_ybrowse_files_and_is_reproducible(tmp_path):
    source = tmp_path / "old.tar.gz"
    sha = _source(source)
    assert _build(source, sha, tmp_path / "a").returncode == 0
    assert _build(source, sha, tmp_path / "b").returncode == 0
    name = "wgsextract-reference-bootstrap-9.9.9.tar.gz"
    built = (tmp_path / "a" / name).read_bytes()
    assert built == (tmp_path / "b" / name).read_bytes()
    digest = hashlib.sha256(built).hexdigest()
    assert (tmp_path / "a/SHA256SUMS").read_text() == f"{digest}  {name}\n"

    with tarfile.open(tmp_path / "a" / name) as tar:
        assert tar.getnames() == [
            "reference/00README.txt",
            "reference/hg19-chm13v2.chain",
        ]
        readme = tar.extractfile("reference/00README.txt").read().decode()  # type: ignore[union-attr]
        chain = tar.extractfile("reference/hg19-chm13v2.chain").read()  # type: ignore[union-attr]
    assert chain == b"chain"
    assert "snps_hg38.vcf.gz (synthetic)" not in readme
    assert "wgsextract ref ybrowse --install" in readme
    assert "Not included since reference bootstrap v9.9.9" in readme
    assert readme.startswith("Liftover chains:\r\n") and "\r\nExome BED:\r\n" in readme
    assert "\n" not in readme.replace("\r\n", "")


def test_bootstrap_build_refuses_an_unpinned_source(tmp_path):
    source = tmp_path / "old.tar.gz"
    _source(source)
    result = _build(source, "0" * 64, tmp_path / "out")
    assert result.returncode != 0
    assert "expected " + "0" * 64 in result.stderr
    assert not (tmp_path / "out").exists()
