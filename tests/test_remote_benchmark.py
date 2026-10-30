"""Microarray kits against a real chip, from public data (run weekly: -m remote).

1000 Genomes sample HG00096 has a 30x whole genome (NYGC, GRCh38) and Illumina
Omni 2.5 chip genotypes. This makes kits from 2 Mb of the genome with the real
CLI and a library from `ref bootstrap`, then compares it with the chip by rsID.
Everything is read remotely by region, so the downloads stay small apart from
the bootstrap (~170 MB) and chr20 (~65 MB).
"""

import json
import sys
from pathlib import Path

import pytest
from pyliftover import LiftOver

from tests.htslib_helpers import needs_tools
from wgsextract_cli.commands.benchmark.chip import (
    CONSUMER_FORMATS,
    read_chip_export,
    score_chip,
)
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.microarray_utils import get_template_format
from wgsextract_cli.core.utils import run_command

pytestmark = [
    pytest.mark.remote,
    needs_tools("bcftools", "samtools", "tabix"),
]

KG = "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp"
GRCH38 = (
    f"{KG}/technical/reference/GRCh38_reference_genome/"
    "GRCh38_full_analysis_set_plus_decoy_hla.fa"
)
HG00096_CRAM = "https://ftp.sra.ebi.ac.uk/vol1/run/ERR324/ERR3240114/HG00096.final.cram"
OMNI = (
    f"{KG}/release/20130502/supporting/hd_genotype_chip/"
    "ALL.chip.omni_broad_sanger_combined.20140818.snps.genotypes.vcf.gz"
)
REGION_HG38 = "chr20:10000000-12000000"
# The same stretch in GRCh37, with a margin; match by rsID, not position.
REGION_HG19 = "20:9900000-12100000"
MIN_MARKERS = 200
MIN_CONCORDANCE = 0.98


def _run(cmd: list[str], cwd: Path) -> str:
    """Run a step (expanding tool wrappers); a failure shows the step's own output."""
    res = run_command(cmd, capture_output=True, check=False, cwd=cwd)
    if res.returncode != 0:
        pytest.fail(
            f"{' '.join(map(str, cmd))} exited {res.returncode}:\n"
            f"{res.stderr or ''}{res.stdout or ''}"
        )
    return str(res.stdout)


def _in_called_region(chrom: str, pos: str, lifter: LiftOver) -> bool:
    hits = lifter.convert_coordinate(f"chr{chrom}", int(pos) - 1)
    if not hits or len(hits) != 1:
        return False
    target, position, strand, _ = hits[0]
    return target == "chr20" and strand == "+" and 9_999_999 <= position < 12_000_000


def test_kit_from_hg00096_matches_its_omni_chip(tmp_path: Path) -> None:
    samtools, bcftools = get_tool_path("samtools"), get_tool_path("bcftools")
    cli = [sys.executable, "-m", "wgsextract_cli.main"]
    library = tmp_path / "reference"
    _run([*cli, "ref", "bootstrap", "--ref", str(library)], tmp_path)
    # chr20 of the reference NYGC aligned to, as the library's genome.
    (library / "genomes").mkdir(exist_ok=True)
    fasta = library / "genomes" / "GRCh38_chr20.fa"
    fasta.write_text(_run([samtools, "faidx", GRCH38, "chr20"], tmp_path), newline="\n")
    _run([samtools, "faidx", str(fasta)], tmp_path)
    # The 2 Mb slice of HG00096's 30x CRAM (htslib fetches it by region).
    bam = tmp_path / "HG00096.bam"
    _run(
        [samtools, "view", "-b", "-T", GRCH38, "-o", str(bam)]
        + [HG00096_CRAM, REGION_HG38],
        tmp_path,
    )
    _run([samtools, "index", str(bam)], tmp_path)

    config = tmp_path / "config.toml"
    config.write_text(f'reference_library = "{library.as_posix()}"\n', newline="\n")
    outdir = tmp_path / "kit"
    _run(
        [*cli, "--config", str(config), "microarray", "--formats"]
        + [
            ",".join(
                name.lower().replace("23andme_snps_api", "23andme_api")
                for name in CONSUMER_FORMATS
            )
        ]
        + ["-r", REGION_HG38, "--input", str(bam), "--ref", str(fasta)]
        + ["--outdir", str(outdir)],
        tmp_path,
    )
    # Independent chip truth, including a different-sample negative control.
    # The query has the same four-column layout as CombinedKit. Missing truth
    # and kit genotypes remain visible rather than disappearing from call rate.
    chip_rows = _run(
        [bcftools, "query", "-s", "HG00096,HG00097", "-r", REGION_HG19]
        + ["-f", "%ID\t%CHROM\t%POS[\t%TGT]\n", OMNI],
        tmp_path,
    )
    truth_rows: list[str] = []
    control_rows: list[str] = []
    lifter = LiftOver(str(library / "hg19ToHg38.over.chain.gz"))
    for row in chip_rows.splitlines():
        rsid, chrom, pos, genotype, control = row.split("\t")
        # Determine eligibility from the independent chip coordinates/chain,
        # never from which markers the generated kit happened to call.
        if rsid.startswith("rs") and _in_called_region(chrom, pos, lifter):
            truth_rows.append(f"{rsid}\t{chrom}\t{pos}\t{genotype}\n")
            control_rows.append(f"{rsid}\t{chrom}\t{pos}\t{control}\n")
    chip_file, control_file = tmp_path / "chip.tsv", tmp_path / "control.tsv"
    chip_file.write_text("".join(truth_rows), newline="\n")
    control_file.write_text("".join(control_rows), newline="\n")
    chip = read_chip_export(chip_file, "CombinedKit")
    control_chip = read_chip_export(control_file, "CombinedKit")
    reports = {}
    for name in ("CombinedKit", *CONSUMER_FORMATS):
        info = get_template_format(name)
        suffix = info["suffix"] if info else ".txt"
        calls = read_chip_export(outdir / f"HG00096_{name}{suffix}", name)
        score = score_chip(calls, chip)
        control = score_chip(calls, control_chip)
        reports[name] = {
            "matched_sample": score.as_dict(),
            "different_sample": control.as_dict(),
        }
    report_file = tmp_path / "chip-concordance.json"
    report_file.write_text(json.dumps(reports, indent=2) + "\n", newline="\n")
    print(json.dumps(reports, indent=2))
    # All layouts must actually have independently measured overlap. A no-call
    # kit must not pass merely because its called-only concordance is undefined.
    for name, report in reports.items():
        matched = report["matched_sample"]["total"]
        negative = report["different_sample"]["total"]
        minimum = (
            MIN_MARKERS
            if name == "CombinedKit"
            else MIN_MARKERS // 2
            if name == "23andMe_V5"
            else 20
        )
        assert matched["chromosome_mismatch"] == 0, name
        assert matched["called"] >= minimum, name
        assert matched["call_rate"] >= 0.90, name
        assert matched["concordance"] >= MIN_CONCORDANCE, name
        assert negative["called"] >= minimum, name
        assert negative["concordance"] < 0.90, name
