import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.build_microarray_chm13_map import write_site_map
from wgsextract_cli.core.microarray_chm13 import CHM13Catalogue, ReferenceContig
from wgsextract_cli.core.microarray_site_map import SiteProjection, load_site_map

from wgsextract_cli.commands import _microarray_chm13 as command
from wgsextract_cli.core import microarray_aadr as aadr

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bcftools", "samtools", "tabix", "bgzip")),
    reason="native variant tools required",
)


@pytest.mark.parametrize("chrom", ["chr1", "1"])
def test_native_vcf_quality_and_unsupported_masks_survive_canonical_aadr_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chrom: str
) -> None:
    fasta = tmp_path / "input.fa"
    fasta.write_text(">chr1\n" + "A" * 100 + "\n")
    subprocess.run(["samtools", "faidx", str(fasta)], check=True)
    mapping = load_site_map(
        write_site_map(
            [
                SiteProjection(
                    "chr1",
                    position,
                    "chr1",
                    position + 10,
                    position == 10,
                    "mapped",
                    "ref_different" if position == 10 else "ref_equal",
                )
                for position in range(10, 15)
            ],
            {"chr1": 100},
            {"chr1": 100},
            tmp_path / "map",
            {},
        )
    )
    catalogue = CHM13Catalogue(
        mapping,
        {"chr1": ReferenceContig(100, hashlib.md5(b"A" * 100).hexdigest())},
        {"chr1": 100},
        (),
        "",
        "",
        "",
    )
    ids = {("chr1", position): f"rs{position}" for position in range(10, 15)}
    monkeypatch.setattr(command, "load_chm13_catalogue", lambda: catalogue)
    monkeypatch.setattr(
        command, "validate_chm13_reference", lambda *_args: {"chr1": "chr1"}
    )
    monkeypatch.setattr(command, "_marker_ids", lambda *_args: ids)
    monkeypatch.setattr(
        aadr,
        "aadr_markers",
        lambda _panel: iter(
            aadr.AADRMarker(
                f"rs{position}",
                "1",
                "0",
                position,
                "C" if position == 10 else "A",
                "T" if position == 10 else "G",
                True,
            )
            for position in range(10, 15)
        ),
    )
    source = tmp_path / "sample.vcf"
    source.write_text(
        "##fileformat=VCFv4.2\n"
        f"##contig=<ID={chrom},length=100>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        '##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Depth">\n'
        '##FORMAT=<ID=GQ,Number=1,Type=Integer,Description="Quality">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        + "".join(
            f"{chrom}\t{position}\t.\tA\t{alt}\t.\tPASS\t.\tGT:DP:GQ\t{gt}:{dp}:30\n"
            for position, alt, gt, dp in (
                (20, "G", "0/1", 10),
                (21, "G", "0/1", 1),
                (22, "G", "0/1", 10),
                (22, "G", "0/0", 10),
                (23, "AT", "0/0", 10),
            )
        )
    )
    out = tmp_path / "outputs"
    args = argparse.Namespace(
        input=str(source),
        region=None,
        ref_vcf_tab=None,
        ploidy_file=None,
        formats="aadr_1240k",
        min_dp=5,
        min_gq=0,
        assume_hom_ref=True,
        exclude_filtered=False,
        call_reasons=True,
        strand_ambiguous="keep",
        resolve_rsids=False,
        parallel=False,
    )
    command.run_chm13(
        args, SimpleNamespace(root=str(tmp_path)), str(fasta), str(out), "1"
    )
    combined = next(out.glob("CHM13_*/*_CombinedKit.txt"))
    assert [line.split("\t")[3] for line in combined.read_text().splitlines()[1:]] == [
        "CT",
        "--",
        "--",
        "--",
        "--",
    ]
    assert (out / "sample_AADR_1240k.geno").read_text().splitlines() == list("19999")
    report = json.loads((out / "sample_CHM13_report.json").read_text())
    assert report["call_reasons"] == {
        "called": 1,
        "low_dp": 1,
        "conflicting_duplicate": 1,
        "unsupported_indel": 1,
        "missing_dp": 1,
    }
    assert not Path(str(source) + ".tbi").exists()
    manifest = tmp_path / "known-indels.csv"
    manifest.write_text(
        "Name,SNP,GenomeBuild,Chr,MapInfo,SourceSeq\n"
        "rs10,[I/D],37,1,10,unverified context\n"
        "rs14,[D/I],37,1,invalid,unverified context\n"
    )
    args.min_dp = 0
    command.run_chm13(
        args,
        SimpleNamespace(root=str(tmp_path)),
        str(fasta),
        str(out),
        "1",
        indel_bindings={"23andMe_V5": str(manifest)},
    )
    assert (out / "sample_AADR_1240k.geno").read_text().splitlines() == list("91999")
    masked_report = json.loads((out / "sample_CHM13_report.json").read_text())
    assert masked_report["asset_summary"]["manifest_indel_ids"] == 2
    assert masked_report["asset_summary"]["quarantined_asset_sites"] == 2
    args.min_dp = 5
    original = combined.read_bytes()
    records = source.read_text().splitlines(keepends=True)
    insertion = next(
        index for index, line in enumerate(records) if line.startswith(f"{chrom}\t20\t")
    )
    records.insert(
        insertion,
        f"{chrom}\t19\t.\tAAA\tA\t.\tPASS\t.\tGT:DP:GQ\t0/1:10:30\n",
    )
    source.write_text("".join(records))
    command.run_chm13(
        args, SimpleNamespace(root=str(tmp_path)), str(fasta), str(out), "1"
    )
    assert combined.read_bytes() == original
    assert (out / "sample_AADR_1240k.geno").read_text().splitlines() == list("99999")
    updated = json.loads((out / "sample_CHM13_report.json").read_text())
    assert updated["call_reasons"]["unsupported_indel"] == 3
    source.write_text(source.read_text().replace("\t20\t.\tA\tG\t", "\t20\t.\tC\tG\t"))
    with pytest.raises(subprocess.CalledProcessError):
        command.run_chm13(
            args, SimpleNamespace(root=str(tmp_path)), str(fasta), str(out), "1"
        )
