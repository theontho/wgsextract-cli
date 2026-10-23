"""Live checks of third-party data the CLI downloads (run weekly: pytest -m remote).

These read a few kilobytes of each remote file, so they catch a moved file or a
changed format without downloading gigabytes.
"""

import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from wgsextract_cli.core.annotation_resources import SPLICEAI_URLS
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import run_command

pytestmark = [
    pytest.mark.remote,
    pytest.mark.skipif(
        not all(get_tool_path(t) for t in ("bcftools", "bgzip", "tabix")),
        reason="bcftools/htslib missing",
    ),
]

SPLICEAI_HG38 = SPLICEAI_URLS["hg38"]
# BRCA1 exon 23's acceptor (17:43045803 C>A scores 1.00 acceptor loss) and a
# neighbour that scores nothing, as published in Ensembl's MANE v1.4 file.
BRCA1_REGION = "17:43045700-43045810"
BRCA1_SCORES = {
    "chr17:43045803:C:A": "A|BRCA1|0.95|1.00|0.00|0.00|-12|-1|-40|-1",
    "chr17:43045700:T:A": "A|BRCA1|0.01|0.00|0.00|0.00|-13|-2|-38|1",
}


def _status(url: str) -> int:
    with urlopen(Request(url, method="HEAD"), timeout=60) as response:
        return int(response.status)


def _run(cmd: list[str], cwd: Path) -> str:
    # run_command expands the wrapper commands get_tool_path may return
    # (wsl:tabix, Pixi launchers).
    return str(run_command(cmd, capture_output=True, cwd=cwd).stdout)


def test_spliceai_hg38_and_its_index_are_published() -> None:
    assert SPLICEAI_HG38
    assert _status(SPLICEAI_HG38) == 200
    assert _status(SPLICEAI_HG38 + ".tbi") == 200


def test_spliceai_hg38_annotates_known_brca1_scores(tmp_path: Path) -> None:
    from wgsextract_cli.commands._analyze_workflows import SPLICEAI_HIGH_EXPR

    tabix, bgzip, bcftools = (get_tool_path(t) for t in ("tabix", "bgzip", "bcftools"))
    assert tabix and bgzip and bcftools and SPLICEAI_HG38
    # tabix fetches the remote index into its working directory.
    sliced = _run([tabix, "-h", SPLICEAI_HG38, BRCA1_REGION], tmp_path)
    assert "##INFO=<ID=SpliceAI," in sliced
    scores = tmp_path / "spliceai_slice.vcf"
    scores.write_text(sliced)
    _run([bgzip, "-f", str(scores)], tmp_path)
    _run([tabix, "-p", "vcf", f"{scores}.gz"], tmp_path)

    # A chr-named sample VCF, as most hg38 callers write them.
    sample = tmp_path / "sample.vcf"
    sample.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr17,length=83257441>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr17\t43045700\t.\tT\tA\t50\tPASS\t.\n"
        "chr17\t43045803\t.\tC\tA\t50\tPASS\t.\n"
    )
    outdir = tmp_path / "out"
    subprocess.run(
        [sys.executable, "-m", "wgsextract_cli.main", "vcf", "spliceai"]
        + ["--input", str(sample), "--spliceai-file", f"{scores}.gz"]
        + ["--outdir", str(outdir)],
        check=True,
    )
    annotated = str(outdir / "spliceai_annotated.vcf.gz")
    rows = _run(
        [bcftools, "query", "-f", "%CHROM:%POS:%REF:%ALT\t%INFO/SpliceAI\n", annotated],
        tmp_path,
    )
    assert dict(line.split("\t") for line in rows.splitlines()) == BRCA1_SCORES

    high = _run(
        [bcftools, "query", "-i", SPLICEAI_HIGH_EXPR, "-f", "%POS\n", annotated],
        tmp_path,
    )
    assert high.split() == ["43045803"]
