import shutil
import subprocess
from pathlib import Path

import pytest

from wgsextract_cli.cli.parser import build_parser
from wgsextract_cli.commands.sign import cmd_create, cmd_verify
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
