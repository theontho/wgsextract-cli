import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.build_microarray_chm13_map import write_site_map
from wgsextract_cli.commands import _microarray_chm13 as command
from wgsextract_cli.core import microarray_aadr as aadr
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.microarray_chm13 import CHM13Catalogue, ReferenceContig
from wgsextract_cli.core.microarray_site_map import SiteProjection, load_site_map
from wgsextract_cli.core.results import CommandResult, collecting
from wgsextract_cli.core.utils import WGSExtractError

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bcftools", "samtools", "tabix", "bgzip")),
    reason="native variant tools required",
)


def _native_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An all-A chr1 whose sites rs10..rs14 sit at native positions 20..24."""
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
        cache_key="toy",
    )
    # The pinned CHM13 site map, the installed vendor templates and the AADR
    # panel are multi-GB downloads; stand in for them with this toy chr1.
    monkeypatch.setattr(command, "load_chm13_catalogue", lambda **_kwargs: catalogue)
    monkeypatch.setattr(
        command, "validate_chm13_reference", lambda *_args: {"chr1": "chr1"}
    )
    (tmp_path / "hg18ToHg19.over.chain.gz").touch()
    monkeypatch.setattr(command, "collect_template_markers", lambda *_args: {})

    def markers(_panel, _reflib_dir=None):
        for position in range(10, 15):
            ref, alt = ("C", "T") if position == 10 else ("A", "G")
            yield aadr.AADRMarker(f"rs{position}", "1", "0", position, ref, alt, True)

    monkeypatch.setattr(aadr, "aadr_markers", markers)
    monkeypatch.setattr(command, "aadr_markers", markers)
    return fasta


@pytest.mark.parametrize("chrom", ["chr1", "1"])
def test_native_vcf_quality_and_unsupported_masks_survive_canonical_aadr_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chrom: str
) -> None:
    fasta = _native_library(tmp_path, monkeypatch)
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
    result = CommandResult(command="microarray")
    with collecting(result):
        returned = command.run_chm13(
            args, SimpleNamespace(root=str(tmp_path)), str(fasta), str(out), "1"
        )
    combined = next(out.glob("CHM13_*/*_CombinedKit.txt"))
    assert returned == combined
    roles = {Path(record.path).name: record.role for record in result.outputs}
    assert roles == {
        "sample_CHM13_report.json": "chm13_report",
        combined.name: "combined_kit",
        **{
            f"sample_AADR_1240k.{ext}": "aadr"
            for ext in ("bed", "bim", "fam", "geno", "ind", "snp")
        },
    }
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


def test_native_gvcf_reference_blocks_are_calls_and_gaps_are_no_calls(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fasta = _native_library(tmp_path, monkeypatch)
    # The marker cache nests two 64-hex names; a shallow library keeps the
    # path under Windows' MAX_PATH, which htslib cannot exceed.
    library = tmp_path_factory.mktemp("lib")
    monkeypatch.setitem(settings, "reference_library", str(library))
    source = tmp_path / "sample.g.vcf"
    source.write_text(
        "##fileformat=VCFv4.2\n"
        "##contig=<ID=chr1,length=100>\n"
        '##INFO=<ID=END,Number=1,Type=Integer,Description="Block end">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        "chr1\t1\t.\tA\t<*>\t.\t.\tEND=21\tGT\t0/0\n"
        "chr1\t22\t.\tA\tG,<*>\t.\tPASS\t.\tGT\t0/1\n"
        "chr1\t23\t.\tA\t<*>\t.\t.\tEND=23\tGT\t./.\n"
    )
    out = tmp_path / "outputs"
    args = argparse.Namespace(
        input=str(source),
        region=None,
        ref_vcf_tab=None,
        ploidy_file=None,
        formats="aadr_1240k",
        min_dp=0,
        min_gq=0,
        assume_hom_ref=False,
        exclude_filtered=False,
        call_reasons=True,
        strand_ambiguous="keep",
        resolve_rsids=False,
        parallel=False,
    )
    command.run_chm13(
        args,
        SimpleNamespace(root=str(tmp_path)),
        str(fasta),
        str(out),
        "1",
        gvcf=True,
    )
    combined = next(out.glob("CHM13_*/*_CombinedKit.txt"))
    calls = [line.split("\t")[3] for line in combined.read_text().splitlines()[1:]]
    # rs10's source strand is complemented; native 0/0 A is a homozygous call.
    assert calls[0][0] == calls[0][1] != "-"
    assert calls[1:] == ["AA", "AG", "--", "--"]
    report = json.loads((out / "sample.g_CHM13_report.json").read_text())
    assert report["call_reasons"] == {
        "called": 3,
        "missing_gt": 1,
        "absent_from_vcf": 1,
    }
    # The marker cache goes in the configured library, not beside --ref.
    assert len(list(library.rglob("markers.tsv"))) == 1
    assert not list(tmp_path.rglob("markers.tsv"))


def test_templates_must_cover_every_pinned_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fasta = _native_library(tmp_path, monkeypatch)
    covered = list(aadr.aadr_markers("1240k"))[:-1]
    monkeypatch.setattr(command, "aadr_markers", lambda *_args: iter(covered))
    args = argparse.Namespace(input=str(tmp_path / "s.vcf"), region=None)
    args.ref_vcf_tab = args.ploidy_file = None
    with pytest.raises(WGSExtractError, match="do not cover the pinned CHM13 map"):
        command.run_chm13(
            args, SimpleNamespace(root=str(tmp_path)), str(fasta), str(tmp_path), "1"
        )
