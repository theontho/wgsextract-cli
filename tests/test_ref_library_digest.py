"""GitHub release digest lookup (the GitHub API is faked) and SHA-256 checks.
download_file's use of them is in test_download_file_http.py."""

import hashlib
import json

import pytest

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

    monkeypatch.setattr(ref_library.downloads, "urlopen", fake_urlopen)

    resolved = ref_library.downloads.resolve_github_release_asset_sha256(
        "https://github.com/WGSExtract/wgsextract-cli/releases/download/v0.1.0/hs38.fa.gz"
    )

    assert resolved == digest
    assert seen_urls == [
        "https://api.github.com/repos/WGSExtract/wgsextract-cli/releases/tags/v0.1.0"
    ]


def test_resolve_github_release_asset_sha256_latest_url(monkeypatch):
    digest = "b" * 64
    seen_urls = []

    def fake_urlopen(request, timeout):
        seen_urls.append(request.full_url)
        return _FakeResponse(
            _asset_payload("wgsextract-reference-bootstrap.tar.gz", digest)
        )

    monkeypatch.setattr(ref_library.downloads, "urlopen", fake_urlopen)

    resolved = ref_library.downloads.resolve_github_release_asset_sha256(
        "https://github.com/WGSExtract/wgsextract-cli/releases/latest/download/wgsextract-reference-bootstrap.tar.gz"
    )

    assert resolved == digest
    assert seen_urls == [
        "https://api.github.com/repos/WGSExtract/wgsextract-cli/releases/latest"
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/theontho/x/releases/download/v1/a.tar.gz",
        "https://github.com/WGSExtract/wgsextract-cli/archive/refs/tags/v1.tar.gz",
        "https://github.com/WGSExtract/wgsextract-cli/releases/tag/v1",
    ],
)
def test_only_github_release_assets_have_a_digest_lookup(url, monkeypatch):
    monkeypatch.setattr(ref_library.downloads, "urlopen", pytest.fail)

    assert ref_library.downloads.resolve_github_release_asset_sha256(url) is None


@pytest.mark.parametrize(
    ("assets", "error"),
    [
        ([{"name": "a.tar.gz", "digest": "md5:abc"}], "did not include a sha256"),
        ([{"name": "a.tar.gz"}], "did not include a sha256"),
        ([{"name": "b.tar.gz", "digest": "sha256:" + "0" * 64}], "was not found"),
    ],
)
def test_github_release_without_a_usable_digest(assets, error, monkeypatch):
    monkeypatch.setattr(
        ref_library.downloads,
        "urlopen",
        lambda request, timeout: _FakeResponse({"assets": assets}),
    )

    with pytest.raises(ValueError, match=error):
        ref_library.downloads.resolve_github_release_asset_sha256(
            "https://github.com/o/r/releases/download/v1/a.tar.gz"
        )


def test_github_digest_lookup_skips_other_assets_and_sends_the_token(monkeypatch):
    digest = "A" * 64
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request.get_header("Authorization"))
        return _FakeResponse(
            {
                "assets": [
                    {"name": "other.tar.gz", "digest": "sha256:" + "0" * 64},
                    {"name": "a b.tar.gz", "digest": f"SHA256:{digest}"},
                ]
            }
        )

    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setattr(ref_library.downloads, "urlopen", fake_urlopen)

    assert (
        ref_library.downloads.resolve_github_release_asset_sha256(
            "https://github.com/o/r/releases/download/v1/a%20b.tar.gz"
        )
        == digest.lower()
    )
    assert seen == ["Bearer t0ken"]


def test_verify_download_sha256(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()

    assert ref_library.downloads.verify_download_sha256(str(path), None)
    assert ref_library.downloads.verify_download_sha256(str(path), digest.upper())
    assert not ref_library.downloads.verify_download_sha256(
        str(tmp_path / "nope"), digest
    )
    assert not ref_library.downloads.verify_download_sha256(str(path), "0" * 64)
    assert not path.exists()
