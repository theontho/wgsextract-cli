import gzip
import io
import tarfile
from argparse import Namespace
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest

from wgsextract_cli.commands.ref import core_commands
from wgsextract_cli.core import genome_library, reference_ncount, reference_resolver
from wgsextract_cli.core.gene_map import GeneMap, _merge_gene_range, gene_map_exists
from wgsextract_cli.core.ref_library import downloads
from wgsextract_cli.core.reference_assets import (
    fasta_signature,
    n_count_paths,
    reference_dict_path,
)
from wgsextract_cli.core.reference_processing import extract_archive_keep_existing
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import WGSExtractError


@pytest.mark.parametrize(
    ("fasta", "expected"),
    [
        ("/r/hg38.fa", "/r/hg38.dict"),
        ("/r/hg38.fa.gz", "/r/hg38.dict"),
        ("/r/x.fasta.gz", "/r/x.dict"),
        ("/r/GCA_000001405.15_GRCh38.fna.gz", "/r/GCA_000001405.15_GRCh38.dict"),
        ("/r/hs38d1.fna", "/r/hs38d1.dict"),
    ],
)
def test_reference_dict_path_strips_fasta_extension(fasta, expected):
    assert reference_dict_path(fasta) == expected


def test_n_count_paths_honor_outdir():
    assert n_count_paths("/lib/genomes/hs38.fa.gz") == (
        "/lib/genomes/hs38_ncnt.csv",
        "/lib/genomes/hs38_nbin.csv",
    )
    assert n_count_paths("/lib/genomes/dog.fna.gz", "/out") == (
        "/out/dog_ncnt.csv",
        "/out/dog_nbin.csv",
    )


@pytest.mark.parametrize(
    ("name", "rank"),
    [
        ("sample_100_R2.fastq.gz", 1),
        ("sample_R1.fq.gz", 0),
        ("sample.2.fastq", 1),
        ("sample_read1.fastq.gz", 0),
        ("sample_S1_L001_R2_001.fastq.gz", 1),
        ("chr1.fastq.gz", 2),
        ("run_12.fastq.gz", 2),
        ("sample_R2_val_2.fq.gz", 1),  # trim_galore
        ("sample_R1.trimmed.fq.gz", 0),
    ],
)
def test_fastq_rank_reads_mate_marker_tokens(name, rank):
    assert genome_library._fastq_rank(Path(name)) == rank


def test_fastq_pair_keys_keep_run_numbers(tmp_path):
    for name in ["run_10_R1.fastq.gz", "run_10_R2.fastq.gz", "run_20_R1.fastq.gz"]:
        (tmp_path / name).write_text("@r\nA\n+\nI\n")

    sets = genome_library._find_fastq_sets(tmp_path)

    assert [(r1.name, r2.name if r2 else None) for r1, r2 in sets] == [
        ("run_10_R1.fastq.gz", "run_10_R2.fastq.gz"),
        ("run_20_R1.fastq.gz", None),
    ]


@pytest.mark.parametrize(
    "pair",
    [
        ("s_R1_val_1.fq.gz", "s_R2_val_2.fq.gz"),
        ("s_R1.trimmed.fq.gz", "s_R2.trimmed.fq.gz"),
        ("s_S1_L001_R1_001.fastq.gz", "s_S1_L001_R2_001.fastq.gz"),
        ("s.1.fq.gz", "s.2.fq.gz"),
    ],
)
def test_fastq_sets_pair_common_mate_naming(tmp_path, pair):
    for name in pair:
        (tmp_path / name).write_text("@r\nA\n+\nI\n")

    sets = genome_library._find_fastq_sets(tmp_path)

    assert [(r1.name, r2.name if r2 else None) for r1, r2 in sets] == [pair]


def test_genome_config_rewrite_keeps_unknown_keys(tmp_path):
    (tmp_path / "sample.cram").write_text("x")
    config_path = tmp_path / genome_library.GENOME_CONFIG_NAME
    config_path.write_text('notes = "keep me"\nthreads = 4\n')

    config = genome_library._load_or_create_genome_config(tmp_path)

    assert config["alignment"] == "sample.cram"
    text = config_path.read_text()
    assert 'alignment = "sample.cram"' in text
    assert 'notes = "keep me"' in text
    assert "threads = 4" in text


def test_gene_map_refuses_t2t_instead_of_using_hg19(tmp_path):
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir()
    (ref_dir / "genes_hg19.tsv").write_text(
        "symbol\tchrom\tstart\tend\nBRCA1\tchr17\t41196312\t41277500\n"
    )

    with pytest.raises(WGSExtractError, match="t2t"):
        GeneMap(str(tmp_path)).get_coords("BRCA1", "t2t")
    assert not gene_map_exists(str(tmp_path), "T2Tv20")
    assert GeneMap(str(tmp_path)).get_coords("BRCA1", "GRCh37") == (
        "chr17:41196312-41277500"
    )


def test_gene_ranges_never_merge_across_contigs():
    gene = _merge_gene_range(None, "chr2_GL383522v1_alt", 107407, 169031)
    gene = _merge_gene_range(gene, "chr2", 168915498, 169031324)
    gene = _merge_gene_range(gene, "chr2", 168900000, 169000000)
    gene = _merge_gene_range(gene, "chr2_GL383522v1_alt", 1, 2)
    assert gene == ("chr2", 168900000, 169031324)


def test_gene_map_ignores_stale_merged_alt_rows(tmp_path):
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir()
    (ref_dir / "genes_hg38.tsv").write_text(
        "symbol\tchrom\tstart\tend\n"
        "ABCB11\tchr2_GL383522v1_alt\t107407\t169031324\n"
        "HLA-X\tchr6_GL000250v2_alt\t1000\t5000\n"
    )
    gm = GeneMap(str(tmp_path))
    assert gm.get_coords("ABCB11", "hg38") is None
    assert gm.get_coords("HLA-X", "hg38") == "chr6_GL000250v2_alt:1000-5000"


def _header(contigs):
    return "".join(f"@SQ\tSN:{name}\tLN:{length}\n" for name, length in contigs)


def test_header_build_detection_uses_chr1_length(monkeypatch):
    t2t = [("chr1", 248387328)] + [(f"chr{i}", 1000) for i in range(2, 26)]
    hg19_wgse = [("chr1", 249250621)] + [(f"chr{i}", 1000) for i in range(2, 26)]
    dog = [(f"NC_0492{i:02d}.1", 1000) for i in range(40)]
    headers = {"t2t.bam": t2t, "hg19.bam": hg19_wgse, "dog.bam": dog}
    monkeypatch.setattr(
        reference_resolver, "get_bam_header", lambda path: _header(headers[path])
    )

    assert ReferenceLibrary._build_from_header("t2t.bam") == "t2t"
    assert ReferenceLibrary._build_from_header("hg19.bam") == "hg19"
    assert ReferenceLibrary._build_from_header("dog.bam") is None


def _write_fasta_with_fai(path: Path, contigs):
    path.write_text("".join(f">{name}\nACGT\n" for name, _ in contigs))
    Path(str(path) + ".fai").write_text(
        "".join(f"{name}\t{length}\t0\t4\t5\n" for name, length in contigs)
    )


def test_annotation_data_resolves_in_a_library_without_a_genome(tmp_path):
    # Annotating a VCF needs no FASTA: a library with only `ref spliceai` (or
    # `ref clinvar`) data must still offer it for the VCF's build.
    vcf = tmp_path / "sample.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=248956422>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
    )
    ref = tmp_path / "lib" / "ref"
    ref.mkdir(parents=True)
    for name in ("spliceai_hg38.vcf.gz", "clinvar_hg38.vcf.gz"):
        (ref / name).write_bytes(b"")

    lib = ReferenceLibrary(str(tmp_path / "lib"), input_path=str(vcf))
    assert (lib.fasta, lib.build) == (None, "hg38")
    assert lib.spliceai_vcf == str(ref / "spliceai_hg38.vcf.gz")
    assert lib.clinvar_vcf == str(ref / "clinvar_hg38.vcf.gz")


def test_reference_library_prefers_fasta_matching_input_signature(tmp_path):
    genomes = tmp_path / "genomes"
    genomes.mkdir()
    _write_fasta_with_fai(genomes / "hg38.fa.gz", [("chr1", 248956422)])
    decoy = [("chr1", 248956422), ("chrUn_JTFH01000001v1_decoy", 25139)]
    _write_fasta_with_fai(genomes / "hs38DH.fa.gz", decoy)
    signature = fasta_signature(str(genomes / "hs38DH.fa.gz"))

    lib = ReferenceLibrary(str(tmp_path), signature, skip_full_search=True)

    assert lib.fasta == str(genomes / "hs38DH.fa.gz")
    assert lib.fai == str(genomes / "hs38DH.fa.gz") + ".fai"
    assert lib.build == "hg38"


def test_reference_beside_the_input_beats_a_library_genome_of_another_build(
    tmp_path,
):
    # The app's test genome ships its own (truncated) hg19 reference, whose
    # header names no build. With hs38 in the library, mt-haplogroup used hs38
    # for the hg19 BAM and Haplogrep found no haplogroup.
    genomes = tmp_path / "lib" / "genomes"
    genomes.mkdir(parents=True)
    _write_fasta_with_fai(genomes / "hs38.fa.gz", [("chr1", 248956422)])
    dataset = tmp_path / "hg19-mini"
    dataset.mkdir()
    mini = [("chr20", 1000001), ("chrY", 500001), ("chrM", 16571)]
    _write_fasta_with_fai(dataset / "hg19-mini.fa.gz", mini)
    bam = dataset / "sample.bam"
    bam.write_bytes(b"")
    signature = fasta_signature(str(dataset / "hg19-mini.fa.gz"))

    lib = ReferenceLibrary(
        str(tmp_path / "lib"), signature, skip_full_search=True, input_path=str(bam)
    )

    assert lib.fasta == str(dataset / "hg19-mini.fa.gz")


def test_reference_library_dict_for_fna_gz_is_not_the_fasta(tmp_path):
    fasta = tmp_path / "GCF_011100685.1_UU_Cfam_GSD_1.0_genomic.fna.gz"
    fasta.write_text(">NC_1\nACGT\n")

    lib = ReferenceLibrary(str(fasta), skip_full_search=True)

    assert lib.fasta == str(fasta)
    assert lib.dict_file is None
    (tmp_path / "GCF_011100685.1_UU_Cfam_GSD_1.0_genomic.dict").write_text("@HD\n")
    lib = ReferenceLibrary(str(fasta), skip_full_search=True)
    assert lib.dict_file == str(
        tmp_path / "GCF_011100685.1_UU_Cfam_GSD_1.0_genomic.dict"
    )


class _FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, code: int = 200):
        super().__init__(data)
        self.code = code

    def getcode(self):
        return self.code

    def info(self):
        return {"Content-Length": str(len(self.getvalue()))}


def _http_416(content_range: str) -> HTTPError:
    headers = Message()
    headers["Content-Range"] = content_range
    return HTTPError(
        "https://example.org/f", 416, "Range Not Satisfiable", headers, None
    )


def _no_curl_download(monkeypatch):
    monkeypatch.setattr(
        downloads, "resolve_github_release_asset_sha256", lambda u: None
    )
    monkeypatch.setattr(downloads, "curl_progress_args", lambda: [])
    monkeypatch.setattr(downloads, "restore_cached_download", lambda *a, **k: False)
    monkeypatch.setattr(downloads, "store_download_in_dev_cache", lambda *a, **k: None)


def test_download_treats_416_for_complete_partial_as_done(tmp_path, monkeypatch):
    _no_curl_download(monkeypatch)
    dest = tmp_path / "file.bin"
    dest.write_bytes(b"complete")
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request.headers.get("Range"), timeout))
        raise _http_416("bytes */8")

    monkeypatch.setattr(downloads, "urlopen", fake_urlopen)

    assert downloads.download_file("https://example.org/f", str(dest))
    assert dest.read_bytes() == b"complete"
    assert not (tmp_path / "file.bin.partial").exists()
    assert requests == [("bytes=8-", downloads.DOWNLOAD_TIMEOUT_SECONDS)]


def test_download_restarts_when_partial_does_not_fit_remote(tmp_path, monkeypatch):
    _no_curl_download(monkeypatch)
    dest = tmp_path / "file.bin"
    (tmp_path / "file.bin.partial").write_bytes(b"0123456789")
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.headers.get("Range"))
        if request.headers.get("Range"):
            raise _http_416("bytes */4")
        return _FakeResponse(b"good")

    monkeypatch.setattr(downloads, "urlopen", fake_urlopen)

    assert downloads.download_file("https://example.org/f", str(dest))
    assert dest.read_bytes() == b"good"
    assert calls == ["bytes=10-", None]


class _ShortResponse(_FakeResponse):
    """A body cut off before its Content-Length, as when a server drops the
    connection (http.client then returns the short body without raising)."""

    def __init__(self, data: bytes, length: int, code: int = 200):
        super().__init__(data, code)
        self.length = length

    def info(self):
        return {"Content-Length": str(self.length)}


def test_download_416_without_size_asks_the_server_for_it(tmp_path, monkeypatch):
    # Ensembl answers a range past the end with a bare 416 (no Content-Range);
    # a complete 27 GB VEP cache was deleted and fetched again from scratch.
    _no_curl_download(monkeypatch)
    dest = tmp_path / "file.bin"
    dest.write_bytes(b"complete")
    methods = []

    def fake_urlopen(request, timeout):
        methods.append(request.get_method())
        if request.get_method() == "HEAD":
            head = _FakeResponse(b"")
            head.headers = {"Content-Length": "8"}
            return head
        raise _http_416("")

    monkeypatch.setattr(downloads, "urlopen", fake_urlopen)

    assert downloads.download_file("https://example.org/f", str(dest))
    assert dest.read_bytes() == b"complete"
    assert methods == ["GET", "HEAD"]


def test_download_resumes_a_cut_off_transfer(tmp_path, monkeypatch):
    _no_curl_download(monkeypatch)
    monkeypatch.setattr(downloads.time, "sleep", lambda s: None)
    dest = tmp_path / "file.bin"
    ranges = []

    def fake_urlopen(request, timeout):
        ranges.append(request.headers.get("Range"))
        if request.headers.get("Range") == "bytes=4-":
            return _FakeResponse(b"lete", code=206)
        return _ShortResponse(b"comp", length=8)

    monkeypatch.setattr(downloads, "urlopen", fake_urlopen)

    assert downloads.download_file("https://example.org/f", str(dest))
    assert dest.read_bytes() == b"complete"
    assert ranges == [None, "bytes=4-"]


def test_download_gives_up_but_keeps_what_it_has(tmp_path, monkeypatch):
    _no_curl_download(monkeypatch)
    monkeypatch.setattr(downloads.time, "sleep", lambda s: None)
    dest = tmp_path / "file.bin"
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.headers.get("Range"))
        if request.headers.get("Range"):
            raise TimeoutError("timed out")
        return _ShortResponse(b"comp", length=8)

    monkeypatch.setattr(downloads, "urlopen", fake_urlopen)

    assert not downloads.download_file("https://example.org/f", str(dest))
    assert not dest.exists()
    assert (tmp_path / "file.bin.partial").read_bytes() == b"comp"
    assert len(calls) == downloads.DOWNLOAD_ATTEMPTS


def _tar_with(tmp_path: Path, members: dict[str, bytes]) -> Path:
    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return archive


def test_archive_extraction_keeps_existing_and_skips_metadata(tmp_path):
    dest = tmp_path / "lib"
    (dest / "reference").mkdir(parents=True)
    (dest / "reference" / "keep.txt").write_text("local")
    archive = _tar_with(
        tmp_path,
        {
            "reference/keep.txt": b"archive",
            "reference/new.txt": b"new",
            "reference/._new.txt": b"appledouble",
        },
    )

    extract_archive_keep_existing(str(archive), str(dest))

    assert (dest / "reference" / "keep.txt").read_text() == "local"
    assert (dest / "reference" / "new.txt").read_text() == "new"
    assert not (dest / "reference" / "._new.txt").exists()


def test_archive_extraction_rejects_path_traversal(tmp_path):
    archive = _tar_with(tmp_path, {"../escape.txt": b"x"})
    with pytest.raises(WGSExtractError, match="Unsafe path"):
        extract_archive_keep_existing(str(archive), str(tmp_path / "lib"))
    assert not (tmp_path / "escape.txt").exists()


def test_n_count_files_match_wgse_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(reference_ncount, "_READ_SIZE", 7)  # force chunk splits
    seq1 = "N" * 400 + "ACGT" * 100 + "NN" + "ACGT" * 25 + "N" * 500
    fasta = tmp_path / "toy.fa.gz"
    lines = [seq1[i : i + 60] for i in range(0, len(seq1), 60)]
    with gzip.open(fasta, "wt") as f:
        f.write(">chrA desc\n" + "\n".join(lines) + "\n>chrB\nACGTACGT\n")
    ncnt, nbin = n_count_paths(str(fasta))

    totals = reference_ncount.write_n_count_files(str(fasta), ncnt, nbin)

    assert (totals.sequences, totals.length, totals.n_count) == (2, 1410, 902)
    ncnt_rows = Path(ncnt).read_text().splitlines()
    assert ncnt_rows[0].startswith("#Processing Ref Model: toy.fa.gz with >300bp")
    chr_a = ncnt_rows[2].split("\t")
    assert chr_a[:8] == ["chrA", "1,402", "902", "2", "450", "71", "1", "1"]
    assert ncnt_rows[3].split("\t")[:3] == ["chrB", "8", "0"]
    assert ncnt_rows[4:] == ["#TOTALS:", "2\t1,410\t902\t2\t\t\t1"]
    assert Path(nbin).read_text().splitlines()[2:] == [
        "chrA\t1\t0\t400",
        "chrA\t2\t902\t500",
    ]


def test_count_ns_writes_sidecars_into_explicit_outdir(tmp_path):
    fasta = tmp_path / "ref.fa"
    fasta.write_text(">chr1\nNNNNACGT\n")
    outdir = tmp_path / "out"

    core_commands.cmd_count_ns(
        Namespace(ref=str(fasta), outdir=str(outdir), _explicit_dests={"outdir"})
    )

    assert (outdir / "ref_ncnt.csv").is_file()
    assert (outdir / "ref_nbin.csv").is_file()
    assert not (tmp_path / "ref_ncnt.csv").exists()


def test_count_ns_requires_ref():
    with pytest.raises(WGSExtractError, match="--ref is required"):
        core_commands.cmd_count_ns(Namespace(ref=None, outdir=None))


def test_ref_verify_raises_on_signature_mismatch(tmp_path, monkeypatch):
    fasta = tmp_path / "hg38.fa.gz"
    _write_fasta_with_fai(fasta, [("chr1", 1000)])
    commands = []
    monkeypatch.setattr(core_commands, "verify_dependencies", lambda deps: None)
    monkeypatch.setattr(core_commands, "log_dependency_info", lambda deps: None)
    monkeypatch.setattr(
        core_commands, "run_command", lambda cmd, **kw: commands.append(cmd)
    )

    with pytest.raises(WGSExtractError, match="does not match the catalog"):
        core_commands.cmd_ref_verify(Namespace(ref=str(fasta)))
    # An existing index is only queried, never rebuilt.
    assert commands[-1] == ["samtools", "faidx", str(fasta), "chr1:1-100"]


@pytest.mark.parametrize("build_hint_name", ["hg38", None])
def test_microarray_target_prefers_all_snps_tab_over_ysnp_vcf(
    tmp_path, build_hint_name
):
    (tmp_path / "snps_hg38.vcf.gz").write_text("y-snps")
    (tmp_path / "snps_hg19.vcf.gz").write_text("y-snps")
    microarray = tmp_path / "microarray"
    microarray.mkdir()
    for build in ["hg19", "hg38", "GRCh38"]:
        (microarray / f"All_SNPs_{build}_ref.tab.gz").write_text("tab")
    fasta_name = "hg38.fa.gz" if build_hint_name else "ref.fa.gz"
    _write_fasta_with_fai(tmp_path / fasta_name, [("chr1", 248956422)])

    lib = ReferenceLibrary(str(tmp_path))

    assert lib.build == "hg38"
    assert lib.ref_vcf_tab == str(microarray / "All_SNPs_hg38_ref.tab.gz")


def test_target_tab_never_uses_another_builds_file(tmp_path):
    from wgsextract_cli.core.reference_assets import find_reference_target_tab

    microarray = tmp_path / "microarray"
    microarray.mkdir()
    (microarray / "All_SNPs_hg19_ref.tab.gz").write_bytes(b"")
    (microarray / "snps_hg38.vcf.gz").write_bytes(b"")

    assert find_reference_target_tab(str(tmp_path), "hg38") == str(
        microarray / "snps_hg38.vcf.gz"
    )
    assert find_reference_target_tab(str(tmp_path), "hg19") == str(
        microarray / "All_SNPs_hg19_ref.tab.gz"
    )
    assert find_reference_target_tab(str(tmp_path), "t2t") == ""


def test_fasta_inside_library_genomes_dir_uses_library_root(tmp_path):
    from wgsextract_cli.core.reference_resolver import ReferenceLibrary

    genomes = tmp_path / "genomes"
    genomes.mkdir()
    fasta = genomes / "hs38.fa.gz"
    fasta.write_bytes(b"")
    (tmp_path / "ploidy.txt").write_text("* * * * 2\n")

    lib = ReferenceLibrary(str(fasta), None)

    assert lib.fasta == str(fasta)
    assert lib.root == str(tmp_path)
    assert lib.ploidy_file == str(tmp_path / "ploidy.txt")
