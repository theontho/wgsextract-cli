import hashlib
import json
from pathlib import Path

from wgsextract_cli.core import ref_library


class _FakeResponse:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._data


def _asset_payload(name: str, digest: str) -> dict:
    return {"assets": [{"name": name, "digest": f"sha256:{digest}"}]}


def test_resolve_github_release_asset_sha256_tagged_url(monkeypatch):
    digest = "a" * 64
    seen_urls = []

    def fake_urlopen(request, timeout):
        seen_urls.append(request.full_url)
        return _FakeResponse(_asset_payload("hs38.fa.gz", digest))

    monkeypatch.setattr(ref_library, "urlopen", fake_urlopen)

    resolved = ref_library.resolve_github_release_asset_sha256(
        "https://github.com/theontho/wgsextract-cli/releases/download/v0.1.0/hs38.fa.gz"
    )

    assert resolved == digest
    assert seen_urls == [
        "https://api.github.com/repos/theontho/wgsextract-cli/releases/tags/v0.1.0"
    ]


def test_resolve_github_release_asset_sha256_latest_url(monkeypatch):
    digest = "b" * 64
    seen_urls = []

    def fake_urlopen(request, timeout):
        seen_urls.append(request.full_url)
        return _FakeResponse(
            _asset_payload("wgsextract-reference-bootstrap.tar.gz", digest)
        )

    monkeypatch.setattr(ref_library, "urlopen", fake_urlopen)

    resolved = ref_library.resolve_github_release_asset_sha256(
        "https://github.com/theontho/wgsextract-cli/releases/latest/download/wgsextract-reference-bootstrap.tar.gz"
    )

    assert resolved == digest
    assert seen_urls == [
        "https://api.github.com/repos/theontho/wgsextract-cli/releases/latest"
    ]


def test_download_file_verifies_github_release_asset_digest(tmp_path, monkeypatch):
    payload = b"reference genome test payload"
    digest = hashlib.sha256(payload).hexdigest()
    dest = tmp_path / "hs38.fa.gz"

    def fake_run_command(cmd, capture_output=False):
        output_path = Path(cmd[cmd.index("-o") + 1])
        output_path.write_bytes(payload)

    def fake_urlopen(request, timeout):
        return _FakeResponse(_asset_payload("hs38.fa.gz", digest))

    monkeypatch.setattr(ref_library, "run_command", fake_run_command)
    monkeypatch.setattr(ref_library, "urlopen", fake_urlopen)

    assert ref_library.download_file(
        "https://github.com/theontho/wgsextract-cli/releases/download/v0.1.0/hs38.fa.gz",
        str(dest),
    )
    assert dest.read_bytes() == payload


def test_download_file_rejects_github_release_asset_digest_mismatch(
    tmp_path, monkeypatch
):
    payload = b"corrupted reference genome test payload"
    expected_digest = hashlib.sha256(b"expected payload").hexdigest()
    dest = tmp_path / "hs38.fa.gz"

    def fake_run_command(cmd, capture_output=False):
        output_path = Path(cmd[cmd.index("-o") + 1])
        output_path.write_bytes(payload)

    def fake_urlopen(request, timeout):
        return _FakeResponse(_asset_payload("hs38.fa.gz", expected_digest))

    monkeypatch.setattr(ref_library, "run_command", fake_run_command)
    monkeypatch.setattr(ref_library, "urlopen", fake_urlopen)

    assert not ref_library.download_file(
        "https://github.com/theontho/wgsextract-cli/releases/download/v0.1.0/hs38.fa.gz",
        str(dest),
    )
    assert not dest.exists()
