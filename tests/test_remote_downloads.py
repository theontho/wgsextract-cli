"""Every file the CLI downloads is still published (run weekly: pytest -m remote).

Asks each server for one byte, so a moved or deleted file shows up here before a
user's download fails. The URLs come from the CLI's own tables.
"""

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from wgsextract_cli.commands.benchmark.models import DEFAULT_REAL_DATASET_URL
from wgsextract_cli.core import annotation_resources as ar
from wgsextract_cli.core import pharmgkb
from wgsextract_cli.core.constants import BOOTSTRAP_URL
from wgsextract_cli.core.gene_map import GENE_MAP_URLS
from wgsextract_cli.core.ref_library.catalog import get_available_genomes

pytestmark = pytest.mark.remote


def _catalog_urls() -> list[str]:
    return [str(g["url"]).strip() for g in get_available_genomes() if g.get("url")]


def _download_urls() -> list[str]:
    tables = [
        ar.CLINVAR_URLS,
        ar.REVEL_URLS,
        ar.PHYLOP_URLS,
        ar.GNOMAD_URLS,
        ar.SPLICEAI_URLS,
        ar.ALPHAMISSENSE_URLS,
        ar.GERP_URLS,
        GENE_MAP_URLS,
    ]
    urls = [url for table in tables for url in table.values() if url]
    urls += [url + ".tbi" for url in ar.SPLICEAI_URLS.values() if url]
    for remote_dir, indel_name in ar.CADD_FILES.values():
        for name in (indel_name, ar.CADD_SNV_FILE):
            urls += [f"{ar.CADD_BASE_URL}/{remote_dir}/{name}"]
    urls += [ar.GNOMAD_V4_URL.format(contig=c) for c in ("chr1", "chrY")]
    urls += [pharmgkb.CLINICAL_URL, pharmgkb.VARIANTS_URL]
    urls += [BOOTSTRAP_URL, DEFAULT_REAL_DATASET_URL]
    return list(dict.fromkeys(urls + _catalog_urls()))


def _first_byte_status(url: str) -> int:
    """The HTTP status of a one-byte request; 200 for an FTP file that sends a byte."""
    request = Request(
        url, headers={"Range": "bytes=0-0", "User-Agent": "wgsextract-cli"}
    )
    try:
        with urlopen(request, timeout=60) as response:
            if url.startswith("ftp://"):
                return 200 if response.read(1) else 404
            return int(response.status)
    except HTTPError as e:
        return e.code


@pytest.mark.parametrize("url", _download_urls())
def test_download_is_published(url: str) -> None:
    try:
        status = _first_byte_status(url)
    except (URLError, TimeoutError) as e:
        pytest.fail(f"{url}: {e}")
    assert status in (200, 206), f"{url} answered HTTP {status}"
