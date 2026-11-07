import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

from wgsextract_cli import api
from wgsextract_cli.commands.sign import _manifest_name, _read_manifest
from wgsextract_cli.core.utils import UsageError, WGSExtractError

pytestmark = pytest.mark.skipif(
    not shutil.which("ssh-keygen"), reason="ssh-keygen not installed"
)


def _create(*paths: Path | str, **options: object) -> api.Result:
    return api.sign.create(
        api.SignCreateParams(paths=tuple(map(Path, paths)), **options)
    )


def _verify(manifest: Path, **options: object) -> api.Result:
    return api.sign.verify(api.SignVerifyParams(manifest=manifest, **options))


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
    created = _create(outputs, key=key)
    manifest = outputs / "SHA256SUMS"
    assert manifest.with_suffix(".sig").exists()
    assert created.metrics == {"files": 2, "signed": True}
    # The manifest is GNU coreutils format.
    if shutil.which("sha256sum"):
        subprocess.run(
            ["sha256sum", "--quiet", "-c", "SHA256SUMS"], cwd=outputs, check=True
        )

    public_key = Path(f"{key}.pub")
    _verify(manifest, public_key=public_key)

    (outputs / "sub" / "b name.vcf").write_text("tampered\n")
    with pytest.raises(api.CommandFailed, match=r"changed: sub/b name\.vcf") as failed:
        _verify(manifest, public_key=public_key)
    assert failed.value.result.metrics["changed_files"] == 1


def test_verify_rejects_other_key_and_edited_manifest(tmp_path, outputs):
    key = _key(tmp_path / "id")
    other = _key(tmp_path / "other")
    _create(outputs, key=key)
    manifest = outputs / "SHA256SUMS"

    with pytest.raises(api.CommandFailed, match="Signature verification FAILED"):
        _verify(manifest, public_key=Path(f"{other}.pub"))

    # Any edit to the signed manifest invalidates the signature.
    manifest.write_text(manifest.read_text().replace("a.txt", "a.txt "))
    with pytest.raises(api.CommandFailed, match="Signature verification FAILED"):
        _verify(manifest, public_key=Path(f"{key}.pub"))


def test_verify_fails_when_ssh_keygen_never_exits(tmp_path, outputs, monkeypatch):
    key = _key(tmp_path / "id")
    _create(outputs, key=key)

    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd, 10, output=b"", stderr=b"incorrect signature\r\n"
        )

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(api.CommandFailed, match="FAILED: incorrect signature$"):
        _verify(outputs / "SHA256SUMS", public_key=Path(f"{key}.pub"))


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
    assert _create(outputs).metrics == {"files": 2, "signed": False}
    manifest = outputs / "SHA256SUMS"
    assert not manifest.with_suffix(".sig").exists()
    verified = _verify(manifest)
    assert [w.code for w in verified.warnings] == ["unsigned"]

    (outputs / "a.txt").unlink()
    with pytest.raises(api.CommandFailed, match="missing: a.txt"):
        _verify(manifest)
    with pytest.raises(api.CommandFailed, match="Signature not found"):
        _verify(manifest, public_key=Path("x.pub"))


def test_unvalidated_signature_is_a_result_warning(tmp_path, outputs):
    # PR #495 review: API clients couldn't tell an intact signature from one
    # checked against a trusted key.
    _create(outputs, key=_key(tmp_path / "id"))
    verified = _verify(outputs / "SHA256SUMS")
    assert verified.metrics["signed"] is True
    assert [w.code for w in verified.warnings] == ["signature_not_trusted"]


def test_verify_reads_nothing_outside_the_read_scope(tmp_path, outputs):
    # PR #495 review: a manifest in an allowed folder could list
    # ../private/file, and <manifest>.sig could link outside, so a server
    # hashed or read files outside its allowed folders.
    from wgsextract_cli.core.read_scope import limited_to

    private = tmp_path / "private"
    private.mkdir()
    (private / "secret").write_text("secret\n")
    _create(outputs / "a.txt", private / "secret", outdir=outputs)
    manifest = outputs / "SHA256SUMS"

    def inside():
        return limited_to(lambda p: Path(p).resolve().is_relative_to(outputs))

    with inside(), pytest.raises(api.CommandFailed, match="Outside the folders"):
        _verify(manifest)

    _create(outputs / "a.txt", outdir=outputs, key=_key(tmp_path / "id"))
    signature = outputs / "SHA256SUMS.sig"
    signature.replace(private / "SHA256SUMS.sig")
    try:
        signature.symlink_to(private / "SHA256SUMS.sig")
    except OSError:
        pytest.skip("can't create symlinks here")
    with inside(), pytest.raises(api.CommandFailed, match="Outside the folders"):
        _verify(manifest)
    assert _verify(manifest).metrics["signed"] is True  # outside a server


def test_create_never_writes_through_a_planted_staging_file(tmp_path, outputs):
    # PR #495 review: a SHA256SUMS.tmp symlink in the output folder made
    # create truncate the file it pointed to.
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    staging = outputs / "SHA256SUMS.tmp"
    try:
        staging.symlink_to(victim)
    except OSError:
        pytest.skip("can't create symlinks here")
    _create(outputs / "a.txt", outdir=outputs)
    assert victim.read_text() == "keep\n"
    assert "a.txt" in (outputs / "SHA256SUMS").read_text()


def test_verify_refuses_two_trust_sources(tmp_path):
    with pytest.raises(UsageError, match="not both"):
        api.SignVerifyParams(
            manifest=tmp_path / "SHA256SUMS",
            public_key=tmp_path / "k.pub",
            allowed_signers=tmp_path / "allowed",
        )


def test_manifest_outside_inputs_keeps_portable_parent_paths(tmp_path, outputs):
    outdir = tmp_path / "manifests"
    _create(outputs, outdir=outdir)
    manifest = outdir / "SHA256SUMS"
    assert [name for _, name in _read_manifest(manifest)] == [
        "../data/a.txt",
        "../data/sub/b name.vcf",
    ]
    _verify(manifest)


def test_manifest_uses_common_input_parent(tmp_path, outputs):
    other = tmp_path / "other.txt"
    other.write_text("gamma\n")
    _create(outputs, other)
    manifest = tmp_path / "SHA256SUMS"
    assert [name for _, name in _read_manifest(manifest)] == [
        "data/a.txt",
        "data/sub/b name.vcf",
        "other.txt",
    ]
    _verify(manifest)


def test_manifest_deduplicates_dot_segment_inputs(tmp_path, outputs):
    alias = outputs / "sub" / ".."
    _create(outputs, alias)
    _create(outputs, alias)
    manifest = outputs / "SHA256SUMS"
    assert [name for _, name in _read_manifest(manifest)] == [
        "a.txt",
        "sub/b name.vcf",
    ]
    _verify(manifest)


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


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows paths required")
@pytest.mark.parametrize("explicit_outdir", [False, True])
def test_manifest_creation_accepts_mixed_case_windows_drives(
    tmp_path, outputs, explicit_outdir
):
    alternate = str(outputs / "sub" / "b name.vcf")
    alternate = alternate[0].swapcase() + alternate[1:]
    destination = tmp_path / "manifests" if explicit_outdir else outputs
    outdir = None
    if explicit_outdir:
        outdir = str(destination)
        outdir = Path(outdir[0].swapcase() + outdir[1:])
    _create(outputs / "a.txt", alternate, outdir=outdir)
    manifest = destination / "SHA256SUMS"
    assert len(_read_manifest(manifest)) == 2
    _verify(manifest)


def test_create_locks_only_the_folder_it_writes(tmp_path, outputs, monkeypatch):
    # PR #495 review: with a configured output folder the job writes there,
    # not into the inputs' common folder.
    from wgsextract_cli.core.config import settings

    params = api.SignCreateParams(paths=(outputs / "a.txt",))
    assert list(params.write_folders()) == [outputs]
    monkeypatch.setitem(settings, "output_directory", str(tmp_path / "results"))
    assert list(params.write_folders()) == []
