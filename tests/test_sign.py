import shutil
import subprocess
from pathlib import Path, PureWindowsPath

import pytest

from wgsextract_cli.cli.parser import build_parser
from wgsextract_cli.commands.sign import (
    _manifest_name,
    _read_manifest,
    cmd_create,
    cmd_verify,
)
from wgsextract_cli.core.utils import WGSExtractError

pytestmark = pytest.mark.skipif(
    not shutil.which("ssh-keygen"), reason="ssh-keygen not installed"
)


def _parse(*argv: str):
    parser, _ = build_parser()
    return parser.parse_args(list(argv))


def _key(path: Path) -> Path:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True
    )
    return path


@pytest.fixture
def outputs(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    (data / "a.txt").write_text("alpha\n")
    (data / "sub" / "b name.vcf").write_text("beta\n")
    return data


def test_signed_manifest_verifies_and_detects_tampering(tmp_path, outputs):
    key = _key(tmp_path / "id")
    cmd_create(_parse("sign", "create", str(outputs), "--key", str(key)))
    manifest = outputs / "SHA256SUMS"
    assert manifest.with_suffix(".sig").exists()
    # The manifest is GNU coreutils format.
    if shutil.which("sha256sum"):
        subprocess.run(
            ["sha256sum", "--quiet", "-c", "SHA256SUMS"], cwd=outputs, check=True
        )

    verify = ("sign", "verify", str(manifest), "--public-key", f"{key}.pub")
    cmd_verify(_parse(*verify))

    (outputs / "sub" / "b name.vcf").write_text("tampered\n")
    with pytest.raises(WGSExtractError, match=r"changed: sub/b name\.vcf"):
        cmd_verify(_parse(*verify))


def test_verify_rejects_other_key_and_edited_manifest(tmp_path, outputs):
    key = _key(tmp_path / "id")
    other = _key(tmp_path / "other")
    cmd_create(_parse("sign", "create", str(outputs), "--key", str(key)))
    manifest = outputs / "SHA256SUMS"

    with pytest.raises(WGSExtractError, match="Signature verification FAILED"):
        cmd_verify(
            _parse("sign", "verify", str(manifest), "--public-key", f"{other}.pub")
        )

    # Any edit to the signed manifest invalidates the signature.
    manifest.write_text(manifest.read_text().replace("a.txt", "a.txt "))
    with pytest.raises(WGSExtractError, match="Signature verification FAILED"):
        cmd_verify(
            _parse("sign", "verify", str(manifest), "--public-key", f"{key}.pub")
        )


def test_manifest_reader_keeps_names_and_accepts_binary_mode(tmp_path):
    digest = "ab" * 32
    manifest = tmp_path / "SHA256SUMS"
    manifest.write_text(f"{digest}  *star.txt\n{digest} *binary mode.bam\n")
    assert _read_manifest(str(manifest)) == [
        (digest, "*star.txt"),
        (digest, "binary mode.bam"),
    ]
    manifest.write_text(f"{digest}\tname\n")
    with pytest.raises(WGSExtractError, match="not a SHA-256 line"):
        _read_manifest(str(manifest))


def test_unsigned_manifest_checks_hashes_only(outputs):
    cmd_create(_parse("sign", "create", str(outputs)))
    manifest = outputs / "SHA256SUMS"
    assert not manifest.with_suffix(".sig").exists()
    cmd_verify(_parse("sign", "verify", str(manifest)))

    (outputs / "a.txt").unlink()
    with pytest.raises(WGSExtractError, match="missing: a.txt"):
        cmd_verify(_parse("sign", "verify", str(manifest)))
    with pytest.raises(WGSExtractError, match="Signature not found"):
        cmd_verify(_parse("sign", "verify", str(manifest), "--public-key", "x.pub"))


def test_manifest_outside_inputs_keeps_portable_parent_paths(tmp_path, outputs):
    outdir = tmp_path / "manifests"
    cmd_create(_parse("sign", "create", str(outputs), "--outdir", str(outdir)))
    manifest = outdir / "SHA256SUMS"
    assert [name for _, name in _read_manifest(manifest)] == [
        "../data/a.txt",
        "../data/sub/b name.vcf",
    ]
    cmd_verify(_parse("sign", "verify", str(manifest)))


def test_manifest_uses_common_input_parent(tmp_path, outputs):
    other = tmp_path / "other.txt"
    other.write_text("gamma\n")
    cmd_create(_parse("sign", "create", str(outputs), str(other)))
    manifest = tmp_path / "SHA256SUMS"
    assert [name for _, name in _read_manifest(manifest)] == [
        "data/a.txt",
        "data/sub/b name.vcf",
        "other.txt",
    ]
    cmd_verify(_parse("sign", "verify", str(manifest)))


def test_manifest_deduplicates_dot_segment_inputs(tmp_path, outputs):
    alias = outputs / "sub" / ".."
    argv = ("sign", "create", str(outputs), str(alias))
    cmd_create(_parse(*argv))
    cmd_create(_parse(*argv))
    manifest = outputs / "SHA256SUMS"
    assert [name for _, name in _read_manifest(manifest)] == [
        "a.txt",
        "sub/b name.vcf",
    ]
    cmd_verify(_parse("sign", "verify", str(manifest)))


@pytest.mark.parametrize(
    ("source", "base"),
    [
        (r"C:\data\sub\result.txt", r"c:\DATA\manifests"),
        (r"\\SERVER\SHARE\data\sub\result.txt", r"\\server\share\data\manifests"),
    ],
)
def test_manifest_names_accept_windows_anchor_case(source, base):
    assert _manifest_name(PureWindowsPath(source), PureWindowsPath(base)) == (
        "../sub/result.txt"
    )


def test_manifest_names_reject_different_windows_drives():
    with pytest.raises(WGSExtractError, match="same drive"):
        _manifest_name(
            PureWindowsPath(r"C:\data\result.txt"), PureWindowsPath(r"D:\out")
        )
