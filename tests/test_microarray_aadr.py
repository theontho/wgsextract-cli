import argparse
import gzip
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyliftover import LiftOver

from scripts.microarray_aadr_markers import build_markers
from tests.htslib_helpers import needs_tools
from wgsextract_cli.commands import microarray
from wgsextract_cli.core import aadr_resources
from wgsextract_cli.core import microarray_aadr as aadr
from wgsextract_cli.core.utils import WGSExtractError, run_command


@pytest.fixture
def markers(tmp_path, monkeypatch):
    source = tmp_path / "markers.tsv.gz"
    with gzip.open(source, "wt") as stream:
        stream.write(
            "# AADR test markers\n"
            "rs1\t1\t0.01\t100\tA\tG\t1\n"
            "rs2\t1\t0.02\t200\tC\tT\t0\n"
            "rs3\t1\t0.03\t300\tG\tA\t1\n"
            "rs4\t1\t0.04\t400\tA\tC\t0\n"
            "rs5\t23\t0.05\t500\tA\tG\t1\n"
        )
    monkeypatch.setattr(aadr, "_MARKERS", source)
    return source


@pytest.fixture
def installed_2m(tmp_path, markers, monkeypatch):
    library = tmp_path / "library"
    source = aadr_resources.aadr_2m_path(str(library))
    source.parent.mkdir(parents=True)
    source.write_text(
        "rs1 1 0.001 100 A G\n"
        "new_probe 1 0.0015 150 T C\n"
        "rs2 1 0.002 200 C T\n"
        "rs3 1 0.003 300 G A\n"
        "rs4 1 0.004 400 A C\n"
        "rs5 23 0.005 500 A G\n"
        "new_probe_x 23 0.0055 550 T C\n"
    )
    monkeypatch.setattr(
        aadr_resources,
        "AADR_2M_SHA256",
        hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    return library


def test_2m_projection_preserves_calls_but_uses_its_own_genetic_map(
    tmp_path, installed_2m
):
    calls = tmp_path / "calls.txt"
    calls.write_text(
        "rs1\t1\t100\tAA\nnew_probe\t1\t150\tTC\n"
        "rs2\t1\t200\tCT\nrs3\t1\t300\tAA\nrs4\t1\t400\tAG\nrs5\tX\t500\tG\n"
    )
    old = str(tmp_path / "old")
    new = str(tmp_path / "new")
    aadr.write_aadr_outputs("1240k", str(calls), old)
    aadr.write_aadr_outputs("2m", str(calls), new, reflib_dir=str(installed_2m))
    old_snps = [line.split() for line in Path(old + ".snp").read_text().splitlines()]
    new_snps = [line.split() for line in Path(new + ".snp").read_text().splitlines()]
    old_geno = Path(old + ".geno").read_text().splitlines()
    new_geno = Path(new + ".geno").read_text().splitlines()
    projected = {
        row[0]: genotype for row, genotype in zip(new_snps, new_geno, strict=True)
    }
    assert [projected[row[0]] for row in old_snps] == old_geno
    assert new_snps[0] == ["rs1", "1", "0.001", "100", "A", "G"]
    assert Path(new + ".bim").read_text().splitlines()[0] == "1\trs1\t0.100\t100\tA\tG"
    assert old_snps[0][2] == "0.01"
    legacy_bytes = {
        ext: Path(old + ext).read_bytes()
        for ext in (".bed", ".bim", ".fam", ".geno", ".snp", ".ind")
    }
    aadr.write_aadr_outputs("1240k", str(calls), old, reflib_dir=str(installed_2m))
    assert all(
        Path(old + ext).read_bytes() == data for ext, data in legacy_bytes.items()
    )


@needs_tools("bgzip", "tabix")
def test_2m_selected_with_ho_builds_the_superset_not_the_legacy_target(
    tmp_path, installed_2m
):
    panels = [aadr.aadr_panel(key) for key in ("AADR-2M", "aadr_ho")]
    assert panels == ["2m", "ho"]
    target = aadr.build_aadr_target(
        panels, str(tmp_path), None, reflib_dir=str(installed_2m)
    )
    with gzip.open(target, "rt") as stream:
        assert "chr1\t150\tnew_probe\n" in stream.read()
    legacy = aadr.build_aadr_target(["ho"], str(tmp_path), None)
    with gzip.open(legacy, "rt") as stream:
        assert "new_probe" not in stream.read()


def test_missing_or_corrupt_2m_never_downloads_or_repairs_at_runtime(
    tmp_path, installed_2m, monkeypatch
):
    monkeypatch.setattr(
        aadr_resources,
        "download_file",
        lambda *a, **kw: pytest.fail("runtime download"),
    )
    with pytest.raises(WGSExtractError, match="not installed"):
        aadr.write_aadr_outputs(
            "2m",
            "not_read",
            str(tmp_path / "missing"),
            reflib_dir=str(tmp_path / "absent"),
        )
    source = aadr_resources.aadr_2m_path(str(installed_2m))
    source.write_text("corrupt\n")
    with pytest.raises(WGSExtractError, match="SHA256 mismatch"):
        list(aadr.aadr_markers("2m", str(installed_2m)))
    assert source.read_text() == "corrupt\n"
    assert not list(tmp_path.glob("missing*"))


def test_explicit_install_uses_verified_shared_fetch_and_is_idempotent(
    tmp_path, monkeypatch
):
    data = b"rs1 1 0.001 100 A G\n"
    monkeypatch.setattr(
        aadr_resources, "AADR_2M_SHA256", hashlib.sha256(data).hexdigest()
    )
    fetched = []

    def fetch(url, dest, **kwargs):
        fetched.append((url, kwargs))
        Path(dest).write_bytes(data)
        return True

    monkeypatch.setattr(aadr_resources, "download_file", fetch)
    installed = aadr_resources.install_aadr_2m(str(tmp_path))
    assert installed.read_bytes() == data
    assert aadr_resources.install_aadr_2m(str(tmp_path)) == installed
    assert len(fetched) == 1
    assert fetched[0][1]["expected_sha256"] == hashlib.sha256(data).hexdigest()


def test_2m_grch38_requires_an_installed_chain_without_auto_fetch(
    tmp_path, monkeypatch
):
    (tmp_path / "hg19ToHg38.over.chain.gz").write_bytes(
        gzip.compress(b"chain 1000 chr1 1000 + 0 500 chr1 1100 + 100 600 1\n500\n\n")
    )
    monkeypatch.setattr(
        aadr, "download_file", lambda *a, **kw: pytest.fail("runtime chain download")
    )
    with pytest.raises(WGSExtractError, match="installed hg19ToHg38"):
        aadr.aadr_lifter("hg38", str(tmp_path), allow_download=False)


def test_mixed_2m_vendor_preflights_chain_before_consumer_target_build(
    tmp_path, installed_2m, monkeypatch
):
    input_file = tmp_path / "input.vcf"
    input_file.touch()
    fasta = tmp_path / "hg38.fa"
    fasta.touch()
    monkeypatch.setattr(microarray, "calculate_bam_md5", lambda *_: "")
    monkeypatch.setattr(microarray, "verify_dependencies", lambda *_: None)
    monkeypatch.setattr(microarray, "log_dependency_info", lambda *_: None)
    monkeypatch.setattr(
        microarray,
        "ReferenceLibrary",
        lambda *a, **kw: SimpleNamespace(
            root=str(installed_2m),
            build="hg38",
            fasta=str(fasta),
            ref_vcf_tab=None,
            ploidy_file=None,
            liftover_chain="unused",
        ),
    )
    monkeypatch.setattr(
        microarray,
        "build_genome_wide_target",
        lambda *a: pytest.fail(
            "consumer target preparation ran before chain preflight"
        ),
    )
    monkeypatch.setattr(
        aadr, "download_file", lambda *a, **kw: pytest.fail("runtime chain download")
    )
    output = tmp_path / "not_created"
    with pytest.raises(WGSExtractError, match="installed hg19ToHg38"):
        microarray.run(
            argparse.Namespace(
                input=str(input_file),
                ref=str(installed_2m),
                formats="aadr_2m,23andme_v5",
                threads="1",
                outdir=str(output),
                ref_vcf_tab=None,
                ploidy_file=None,
            )
        )
    assert not output.exists()


@needs_tools("bcftools", "bgzip", "tabix", "samtools")
@pytest.mark.parametrize("chroms", [("chr1", "chrX"), ("1", "X")])
def test_sparse_vcf_2m_only_hit_does_not_reference_fill_legacy_panels(
    tmp_path, installed_2m, monkeypatch, chroms
):
    fasta = tmp_path / "hg19.fa"
    sequence = list("A" * 600)
    sequence[199], sequence[299], sequence[549] = "C", "G", "T"
    fasta.write_text("".join(f">{chrom}\n{''.join(sequence)}\n" for chrom in chroms))
    run_command(["samtools", "faidx", str(fasta)])
    plain = tmp_path / "input.vcf"
    plain.write_text(
        "##fileformat=VCFv4.2\n"
        + "".join(f"##contig=<ID={chrom},length=600>\n" for chrom in chroms)
        + '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n"
        f"{chroms[0]}\t100\trs1\tA\tG\t.\tPASS\t.\tGT\t1/1\n"
        f"{chroms[1]}\t550\tnew_probe_x\tT\tC\t.\tPASS\t.\tGT\t0/1\n"
    )
    compressed = str(plain) + ".gz"
    with open(compressed, "wb") as stream:
        run_command(["bgzip", "-c", str(plain)], stdout=stream)
    run_command(["tabix", "-p", "vcf", compressed])
    lib = SimpleNamespace(
        root=str(installed_2m),
        build="hg19",
        fasta=str(fasta),
        ref_vcf_tab=None,
        ploidy_file=None,
        liftover_chain=None,
    )
    monkeypatch.setattr(microarray, "ReferenceLibrary", lambda *a, **kw: lib)
    args = argparse.Namespace(
        input=compressed,
        ref=str(fasta),
        outdir=str(tmp_path / "legacy"),
        threads="1",
        region=None,
        formats="aadr_1240k,aadr_ho",
        parallel=False,
        ref_vcf_tab=None,
        ploidy_file=None,
    )
    microarray.run(args)
    baseline = {
        path.name: path.read_bytes()
        for path in (tmp_path / "legacy").glob("input_AADR_*")
    }
    args.formats = "aadr_2m,aadr_1240k,aadr_ho"
    args.outdir = str(tmp_path / "mixed")
    microarray.run(args)
    assert len(baseline) == 12
    assert all(
        (tmp_path / "mixed" / name).read_bytes() == data
        for name, data in baseline.items()
    )
    assert (tmp_path / "mixed/input_AADR_ho.geno").read_text().splitlines()[-1] == "9"
    snps = (tmp_path / "mixed/input_AADR_2m.snp").read_text().splitlines()
    genotypes = (tmp_path / "mixed/input_AADR_2m.geno").read_text().splitlines()
    projected = {
        snp.split()[0]: genotype for snp, genotype in zip(snps, genotypes, strict=True)
    }
    assert projected["rs5"] == "2"
    assert projected["new_probe_x"] == "1"


@pytest.mark.parametrize("build", ["t2t", "unknown"])
def test_unsupported_2m_build_fails_before_data_fetch_or_calling(
    tmp_path, monkeypatch, build
):
    input_file = tmp_path / "input.bam"
    input_file.touch()
    monkeypatch.setattr(microarray, "calculate_bam_md5", lambda *_: "")
    monkeypatch.setattr(microarray, "verify_dependencies", lambda *_: None)
    monkeypatch.setattr(microarray, "log_dependency_info", lambda *_: None)
    monkeypatch.setattr(
        microarray,
        "ReferenceLibrary",
        lambda *a, **kw: SimpleNamespace(
            root=str(tmp_path),
            build=build,
            fasta="not_read",
            ref_vcf_tab=None,
            ploidy_file=None,
            liftover_chain=None,
        ),
    )
    monkeypatch.setattr(
        microarray, "require_aadr_2m", lambda *_: pytest.fail("must reject build first")
    )
    output = tmp_path / "not_created"
    with pytest.raises(WGSExtractError, match="2M requires GRCh37 or GRCh38"):
        microarray.run(
            argparse.Namespace(
                input=str(input_file),
                ref=str(tmp_path),
                formats="aadr_2m",
                threads="1",
                outdir=str(output),
                ref_vcf_tab=None,
                ploidy_file=None,
            )
        )
    assert not output.exists()


def test_outputs_preserve_aadr_alleles_order_and_reference_dosage(tmp_path, markers):
    calls = tmp_path / "kit.txt"
    calls.write_text(
        "# RSID CHROM POS RESULT\n"
        "different_id\tchr1\t100\tAA\n"
        "rs2\t1\t200\tTC\n"
        "rs3\t1\t300\tAA\n"
        "rs4\t1\t400\tAG\n"
        "rs5\tX\t500\tG\n"
    )
    prefix = str(tmp_path / "kit")
    counts = aadr.write_aadr_outputs("1240k", str(calls), prefix, sex="M")
    assert counts == {"markers": 5, "called": 4, "incompatible": 1}
    assert Path(prefix + ".geno").read_text() == "2\n1\n0\n9\n0\n"
    assert Path(prefix + ".bed").read_bytes() == b"\x6c\x1b\x01\x00\x02\x03\x01\x03"
    assert Path(prefix + ".snp").read_text().splitlines()[:2] == [
        "rs1\t1\t0.01\t100\tA\tG",
        "rs2\t1\t0.02\t200\tC\tT",
    ]
    assert (
        Path(prefix + ".bim").read_text().splitlines()[0] == "1\trs1\t1.00\t100\tA\tG"
    )
    assert Path(prefix + ".fam").read_text() == "0 sample 0 0 1 -9\n"
    assert Path(prefix + ".ind").read_text() == "sample\tM\tUnknown\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_ho_filters_without_reordering_and_missing_sites_are_not_imputed(
    tmp_path, markers
):
    calls = tmp_path / "kit.txt"
    calls.write_text("rs1\t1\t100\tAG\n")
    prefix = str(tmp_path / "ho")
    aadr.write_aadr_outputs("ho", str(calls), prefix)
    assert Path(prefix + ".geno").read_text() == "1\n9\n9\n"
    assert [
        line.split()[0] for line in Path(prefix + ".snp").read_text().splitlines()
    ] == ["rs1", "rs3", "rs5"]
    assert list(aadr.aadr_markers("1240k_ho")) == list(aadr.aadr_markers("1240k"))


@pytest.mark.parametrize("genotype", ["--", "A-", "NN", "AT", "", "AAA"])
def test_invalid_or_out_of_panel_alleles_are_missing(genotype):
    assert aadr.reference_dosage(genotype, "A", "G") == "9"


@pytest.mark.parametrize("sample_id", ["", "../sample", "a b", "a\nb", "é", "a" * 40])
def test_unsafe_sample_labels_fail_before_output(tmp_path, sample_id):
    with pytest.raises(WGSExtractError, match="aadr-sample-id"):
        aadr.write_aadr_outputs(
            "ho", "nonexistent", str(tmp_path / "kit"), sample_id=sample_id
        )
    assert not list(tmp_path.iterdir())


def test_conflicting_calls_at_one_coordinate_are_missing(tmp_path, markers):
    calls = tmp_path / "kit.txt"
    calls.write_text("rs1\t1\t100\tAA\nother\tchr1\t100\tGG\n")
    prefix = str(tmp_path / "conflict")
    aadr.write_aadr_outputs("ho", str(calls), prefix)
    assert Path(prefix + ".geno").read_text() == "9\n9\n9\n"


def test_grch38_forward_mapping_and_inversion_keep_grch37_output(tmp_path, markers):
    chain = tmp_path / "hg19ToHg38.chain"
    chain.write_text("chain 1000 chr1 1000 + 0 1000 chr1 1100 - 0 1000 1\n1000\n\n")
    lifter = LiftOver(str(chain))
    calls = tmp_path / "kit.txt"
    # GRCh37 A/G -> GRCh38 T/C on the inverted chain.
    calls.write_text("rs1\t1\t1001\tTT\nrs2\t1\t901\tGA\n")
    prefix = str(tmp_path / "inverted")
    aadr.write_aadr_outputs("1240k", str(calls), prefix, lifter=lifter)
    assert Path(prefix + ".geno").read_text() == "2\n1\n9\n9\n9\n"
    assert (
        Path(prefix + ".snp").read_text().splitlines()[0] == "rs1\t1\t0.01\t100\tA\tG"
    )


def test_ambiguous_or_alt_contig_liftover_is_not_used(markers):
    marker = next(aadr.aadr_markers("1240k"))

    class Ambiguous:
        def convert_coordinate(self, *_):
            return [("chr1", 99, "+"), ("chr2", 99, "+")]

    class Alternate:
        def convert_coordinate(self, *_):
            return [("chr1_alt", 99, "+")]

    assert aadr._marker_coordinate(marker, Ambiguous()) is None
    assert aadr._marker_coordinate(marker, Alternate()) is None


def test_installed_chain_is_reused_read_only_without_a_download(
    tmp_path, markers, monkeypatch
):
    library = tmp_path / "library"
    library.mkdir()
    chain = library / "hg19ToHg38.over.chain.gz"
    chain.write_bytes(
        gzip.compress(b"chain 1000 chr1 1000 + 0 500 chr1 1100 + 100 600 1\n500\n\n")
    )
    monkeypatch.setattr(
        aadr, "download_file", lambda *_: pytest.fail("must not download")
    )
    out = tmp_path / "not_created"
    lifter = aadr.aadr_lifter("hg38", str(out), str(library))
    marker = next(aadr.aadr_markers("1240k"))
    assert aadr._marker_coordinate(marker, lifter) == ("1", 200, False)
    assert not out.exists()


@needs_tools("bgzip", "tabix")
@pytest.mark.parametrize("suffix", [".gz", ".bgz"])
def test_separate_target_keeps_legacy_ids_and_does_not_modify_source(
    tmp_path, markers, suffix
):
    original = tmp_path / ("legacy.tab" + suffix)
    with gzip.open(original, "wt") as stream:
        stream.write("#CHROM\tPOS\tID\nchr1\t100\told_id\nchr20\t100\trs20\n")
    before = original.read_bytes()
    target = aadr.build_aadr_target(["ho"], str(tmp_path), None, str(original))
    assert original.read_bytes() == before
    with gzip.open(target, "rt") as stream:
        text = stream.read()
    assert "chr1\t100\told_id\n" in text
    assert "chr1\t300\trs3\n" in text
    assert "chrX\t500\trs5\n" in text
    assert "chr20\t100\trs20\n" in text
    assert "chr1\t200\trs2\n" not in text
    before_target = Path(target).read_bytes()
    second = aadr.build_aadr_target(["1240k"], str(tmp_path), None)
    assert second != target
    assert Path(target).read_bytes() == before_target
    assert Path(target + ".tbi").is_file()
    with gzip.open(second, "rt") as stream:
        assert "chr1\t200\trs2\n" in stream.read()


def test_marker_builder_rejects_panel_conflicts_and_has_no_genotypes(tmp_path):
    panel = tmp_path / "1240k.snp"
    ho = tmp_path / "ho.snp"
    out = tmp_path / "out.tsv.gz"
    panel.write_text("rs1 1 0.01 100 A G\nrs2 2 0.02 200 C T\n")
    ho.write_text("rs1 1 0.01 100 A G\n")
    assert build_markers(panel, ho, out) == 2
    with gzip.open(out, "rt") as stream:
        assert stream.read().splitlines()[1:] == [
            "rs1\t1\t0.01\t100\tA\tG\t1",
            "rs2\t2\t0.02\t200\tC\tT\t0",
        ]
    first = out.read_bytes()
    build_markers(panel, ho, out)
    assert out.read_bytes() == first
    ho.write_text("rs1 1 0.01 100 G A\n")
    with pytest.raises(ValueError, match="Panels disagree"):
        build_markers(panel, ho, out)
    assert out.read_bytes() == first


def test_aadr_dispatch_uses_input_build_calls_without_reverse_chain(
    tmp_path, markers, monkeypatch
):
    calls = tmp_path / "input_CombinedKit.txt"
    calls.write_text("rs1\t1\t100\tAA\n")
    monkeypatch.setattr(microarray, "aadr_lifter", lambda *_: None)
    monkeypatch.setattr(microarray, "_template_search_roots", lambda *_: [])
    microarray._convert_microarray_outputs(
        args=argparse.Namespace(formats="AADR-HO,aadr_ho"),
        outdir=str(tmp_path),
        base_name="input",
        lib=SimpleNamespace(build="hg38", liftover_chain=None),
        combined_kit_txt=str(calls),
        ref_fasta="unused",
        start_total=time.time(),
    )
    assert (tmp_path / "input_AADR_ho.geno").read_text() == "2\n9\n9\n"
    assert not (tmp_path / "input_CombinedKit_hg19.txt").exists()


@pytest.mark.parametrize("build", [None, "t2t", "unknown"])
def test_aadr_requires_a_known_supported_build(tmp_path, build):
    with pytest.raises(WGSExtractError, match="known GRCh37 or GRCh38"):
        aadr.aadr_lifter(build, str(tmp_path))


def test_a_bad_marker_table_leaves_no_partial_outputs(tmp_path, markers):
    calls = tmp_path / "calls.txt"
    calls.write_text("")
    with gzip.open(markers, "at") as stream:
        stream.write("malformed\n")
    with pytest.raises(ValueError):
        aadr.write_aadr_outputs("ho", str(calls), str(tmp_path / "broken"))
    assert not list(tmp_path.glob("broken*"))


@needs_tools("bcftools", "bgzip", "tabix", "samtools")
@pytest.mark.parametrize(
    ("records", "expected", "policy"),
    [
        (
            "chr1\t100\trs1\tA\tG\t.\tPASS\t.\tGT\t0/0\n"
            "chr1\t100\trs1\tA\tG\t.\tPASS\t.\tGT\t1/1\n"
            "chr1\t300\trs3\tG\tA\t.\tPASS\t.\tGT\t0/1\n",
            "9\n1\n9\n",
            {},
        ),
        (
            "chr1\t100\trs1\tA\tG\t.\tPASS\t.\tGT\t0/0\n"
            "chr1\t300\trs3\tG\tGA\t.\tPASS\t.\tGT\t1/1\n",
            "2\n9\n9\n",
            {},
        ),
        (
            "chr1\t100\trs1\ta\tg\t.\tPASS\t.\tGT\t0/1\n"
            "chr1\t300\trs3\tG\tA\t.\tPASS\t.\tGT\t0/1\n",
            "1\n1\n9\n",
            {},
        ),
        (
            "chr1\t100\trs1\tA\tG\t.\tPASS\t.\tGT:GQ:DP\t0/0:10:50\n"
            "chr1\t300\trs3\tG\tA\t.\tPASS\t.\tGT:GQ:DP\t0/1:99:50\n",
            "9\n1\n9\n",
            {"min_gq": 20},
        ),
        (
            "chr1\t100\trs1\tA\tG\t.\tPASS\t.\tGT:GQ:DP\t0/0:99:3\n"
            "chr1\t300\trs3\tG\tA\t.\tPASS\t.\tGT:GQ:DP\t0/1:99:50\n",
            "9\n1\n9\n",
            {"min_dp": 10},
        ),
    ],
)
def test_cli_vcf_rejected_calls_cannot_be_reference_filled(
    tmp_path, markers, monkeypatch, records, expected, policy
):
    fasta = tmp_path / "hg19.fa"
    sequence = list("A" * 500)
    sequence[299] = "G"
    fasta.write_text(">chr1\n" + "".join(sequence) + "\n")
    run_command(["samtools", "faidx", str(fasta)])
    plain = tmp_path / "input.vcf"
    plain.write_text(
        "##fileformat=VCFv4.2\n"
        "##contig=<ID=chr1,length=500>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        '##FORMAT=<ID=GQ,Number=1,Type=Integer,Description="Genotype quality">\n'
        '##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Depth">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n" + records
    )
    compressed = str(plain) + ".gz"
    with open(compressed, "wb") as stream:
        run_command(["bgzip", "-c", str(plain)], stdout=stream)
    run_command(["tabix", "-p", "vcf", compressed])
    lib = SimpleNamespace(
        root=None,
        build="hg19",
        fasta=str(fasta),
        ref_vcf_tab=None,
        ploidy_file=None,
        liftover_chain=None,
    )
    monkeypatch.setattr(microarray, "ReferenceLibrary", lambda *_, **__: lib)
    out = tmp_path / "output"
    args = argparse.Namespace(
        input=compressed,
        ref="unused",
        outdir=str(out),
        threads="1",
        region="chr1:1-400",
        formats="aadr_ho",
        parallel=False,
        ref_vcf_tab=None,
        ploidy_file=None,
        **policy,
    )
    microarray.run(args)
    assert (out / "input_AADR_ho.geno").read_text() == expected
    working = list(out.glob("AADR_*/AADR_targets_*.tab.gz"))
    assert len(working) == 1
    assert working[0].is_file()
    first_kit = next(out.glob("AADR_*/input_CombinedKit.txt"))
    first_bytes = first_kit.read_bytes()
    args.formats = "aadr_1240k"
    microarray.run(args)
    assert first_kit.read_bytes() == first_bytes
    assert len(list(out.glob("AADR_*/input_CombinedKit.txt"))) == 2
    assert len((out / "input_AADR_1240k.geno").read_text().splitlines()) == 5
    assert (out / "input_AADR_ho.geno").read_text() == expected
