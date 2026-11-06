"""`wgsextract ref` index/count-ns/verify/library commands on tiny references."""

import gzip
import hashlib
import io
import logging
import random
import sys
import zipfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.htslib_helpers import bgzip_index, needs_tools, vcf_calls, write_fasta
from tests.http_server import serving
from wgsextract_cli import api
from wgsextract_cli.api import (
    RefCountNsParams,
    RefGeneMapParams,
    RefIndexParams,
    RefLibraryParams,
    RefVerifyParams,
)
from wgsextract_cli.commands.ref import core_commands, library_commands
from wgsextract_cli.core import (
    annotation_resources,
    bwa_mem2,
    config,
    constants,
    gnomad_af,
    pharmgkb,
)
from wgsextract_cli.core.annotation_resources import (
    ALPHAMISSENSE_URLS,
    CLINVAR_URLS,
    GNOMAD_URLS,
    REVEL_URLS,
    SPLICEAI_URLS,
)
from wgsextract_cli.core.bigwig_resources import GERP_URLS, PHYLOP_URLS
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.gene_map import GENE_MAP_URLS
from wgsextract_cli.core.reference_assets import (
    read_fai_lengths,
    reference_dict_path,
    sequence_signature,
)
from wgsextract_cli.core.utils import run_command
from wgsextract_cli.main import main


def _bgzipped_fasta(tmp_path: Path, seqs: dict[str, str], name="ref.fa") -> Path:
    plain = tmp_path / name
    plain.write_text("".join(f">{n}\n{s}\n" for n, s in seqs.items()))
    out = tmp_path / f"{name}.gz"
    with open(out, "wb") as f:
        run_command([get_tool_path("bgzip"), "-c", str(plain)], stdout=f)
    plain.unlink()
    return out


def _random_seqs() -> dict[str, str]:
    rng = random.Random(3)
    return {
        "chr1": "".join(rng.choice("ACGT") for _ in range(3000)),
        "chr2": "".join(rng.choice("ACGT") for _ in range(1200)),
    }


# --- ref index ---------------------------------------------------------------


@needs_tools("samtools", "bgzip", "bwa")
def test_index_builds_fai_dict_and_bwa_index(tmp_path):
    ref = _bgzipped_fasta(tmp_path, _random_seqs())

    result = api.ref.index(RefIndexParams(ref=ref, aligner="bwa"))

    assert read_fai_lengths(ref) == {"chr1": 3000, "chr2": 1200}
    indexes = {Path(o.path).name for o in result.outputs if o.role == "reference_index"}
    assert {"ref.dict", "ref.fa.gz.fai", "ref.fa.gz.bwt"} <= indexes
    assert (tmp_path / "ref.fa.gz.gzi").is_file()
    sq = [
        line.split("\t")[1:3]
        for line in (tmp_path / "ref.dict").read_text().splitlines()
        if line.startswith("@SQ")
    ]
    assert sq == [["SN:chr1", "LN:3000"], ["SN:chr2", "LN:1200"]]
    for suffix in (".amb", ".ann", ".bwt", ".pac", ".sa"):
        assert (tmp_path / f"ref.fa.gz{suffix}").stat().st_size > 0
    assert not bwa_mem2.has_index(str(ref))


@needs_tools("samtools", "bgzip", "bwa-mem2")
def test_index_with_bwa_mem2_builds_its_index_not_bwas(tmp_path, monkeypatch):
    ref = _bgzipped_fasta(tmp_path, _random_seqs())
    monkeypatch.setattr(bwa_mem2, "download_file", pytest.fail)

    api.ref.index(RefIndexParams(ref=ref, aligner="bwa-mem2"))

    assert (tmp_path / "ref.dict").is_file()
    assert bwa_mem2.has_index(str(ref))
    assert not (tmp_path / "ref.fa.gz.sa").exists()  # bwa's index wasn't built


@needs_tools("samtools", "bgzip", "bwa-mem2")
def test_index_bwa_mem2_without_memory_fails_after_the_fai_and_dict(
    tmp_path, monkeypatch
):
    ref = _bgzipped_fasta(tmp_path, _random_seqs())
    monkeypatch.setattr(bwa_mem2, "_total_memory", lambda: 0)

    with pytest.raises(api.CommandFailed, match="Indexing failed: .*--aligner bwa"):
        api.ref.index(RefIndexParams(ref=ref, aligner="bwa-mem2"))

    assert (tmp_path / "ref.fa.gz.fai").is_file()
    assert (tmp_path / "ref.dict").is_file()
    assert not bwa_mem2.has_index(str(ref))


@needs_tools("samtools", "bwa")
def test_index_of_plain_gzip_fasta_reports_the_samtools_failure(tmp_path):
    ref = tmp_path / "ref.fa.gz"
    with gzip.open(ref, "wt") as f:  # gzip, not bgzip: samtools can't index it
        f.write(">chr1\nACGTACGT\n")

    with pytest.raises(api.CommandFailed, match="Indexing failed"):
        api.ref.index(RefIndexParams(ref=ref, aligner="bwa"))
    assert not (tmp_path / "ref.fa.gz.bwt").exists()


@needs_tools("samtools", "bwa")
@pytest.mark.parametrize(
    ("ref", "message"), [(None, "--ref is required"), ("nope.fa", "file not found")]
)
def test_index_needs_an_existing_ref(tmp_path, ref, message):
    ref = ref and tmp_path / ref
    with pytest.raises(api.CommandFailed, match=message):
        api.ref.index(RefIndexParams(ref=ref))


# --- ref count-ns ------------------------------------------------------------


@needs_tools("bgzip")
def test_count_ns_counts_known_runs_beside_the_fasta(tmp_path, caplog):
    # chr1: a 400 bp N run (>300 bp) and a 3 bp one; chr2 has none.
    chr1 = "N" * 400 + "ACGT" * 50 + "NNN" + "ACGT" * 50
    ref = _bgzipped_fasta(tmp_path, {"chr1": chr1, "chr2": "ACGT" * 10})
    outdir = tmp_path / "configured-output"

    with caplog.at_level(logging.INFO):
        core_commands.cmd_count_ns(
            # A configured output_directory, not --outdir, must not move the files.
            Namespace(ref=str(ref), outdir=str(outdir), _explicit_dests=set())
        )

    assert not outdir.exists()
    rows = (tmp_path / "ref_ncnt.csv").read_text().splitlines()
    assert rows[2].split("\t")[:3] == ["chr1", "803", "403"]
    assert rows[3].split("\t")[:3] == ["chr2", "40", "0"]
    assert rows[-1].split("\t")[:3] == ["2", "843", "403"]
    assert (tmp_path / "ref_nbin.csv").is_file()
    assert "2 sequences, 843 bp, 403 N (47.81%) in 1 runs >300 bp" in caplog.text


def test_count_ns_on_a_corrupt_fasta_suggests_outdir(tmp_path):
    ref = tmp_path / "ref.fa.gz"
    ref.write_bytes(b"not gzip at all")
    with pytest.raises(api.CommandFailed, match="N-counting failed: .*Pass --outdir"):
        api.ref.count_ns(RefCountNsParams(ref=ref))


# --- ref verify --------------------------------------------------------------


@needs_tools("samtools", "bgzip")
def test_verify_passes_an_indexed_bgzipped_fasta(tmp_path, caplog):
    seqs = _random_seqs()
    ref = _bgzipped_fasta(tmp_path, seqs)
    run_command(["samtools", "faidx", str(ref)])
    run_command(["samtools", "dict", str(ref), "-o", str(tmp_path / "ref.dict")])

    with caplog.at_level(logging.INFO):
        api.ref.verify(RefVerifyParams(ref=ref))

    signature = sequence_signature({n: len(s) for n, s in seqs.items()})
    assert f"Sequence dictionary signature: {signature}" in caplog.text
    assert "Reference ref.fa.gz appears to be valid." in caplog.text
    assert "No sequence dictionary" not in caplog.text


@needs_tools("samtools", "bgzip")
def test_verify_fails_a_truncated_gzip(tmp_path):
    ref = _bgzipped_fasta(tmp_path, _random_seqs())
    data = ref.read_bytes()
    ref.write_bytes(data[: len(data) // 2])
    with pytest.raises(api.CommandFailed, match="Gzip integrity check failed"):
        api.ref.verify(RefVerifyParams(ref=ref))


@needs_tools("samtools")
def test_verify_indexes_a_plain_fasta_and_warns_without_a_dict(tmp_path, caplog):
    ref = tmp_path / "ref.fa"
    ref.write_text(">chrM\nACGTNNACGT\n")

    with caplog.at_level(logging.INFO):
        api.ref.verify(RefVerifyParams(ref=ref))

    assert read_fai_lengths(ref) == {"chrM": 10}
    assert "Gzip" not in caplog.text
    assert f"No sequence dictionary {tmp_path / 'ref.dict'}" in caplog.text


@needs_tools("samtools")
@pytest.mark.parametrize(
    "fai",
    [
        "chr1\t40\t100000\t40\t41\n",  # points past the end of the FASTA
        "chr9\t40\t6\t40\t41\n",  # another file's contig, still fetchable
    ],
)
def test_verify_fails_a_stale_fai(tmp_path, fai):
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": "ACGT" * 10})
    Path(f"{ref}.fai").write_text(fai)
    with pytest.raises(api.CommandFailed, match="ref.fa.fai doesn't match ref.fa"):
        api.ref.verify(RefVerifyParams(ref=ref))
    assert Path(f"{ref}.fai").read_text() == fai  # left for the user to remove
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


@needs_tools("samtools", "bgzip")
def test_verify_fails_a_gzi_from_another_file(tmp_path):
    ref = _bgzipped_fasta(tmp_path, _random_seqs())
    other = _bgzipped_fasta(tmp_path, {"chr1": "ACGT" * 50000}, name="other.fa")
    run_command(["samtools", "faidx", str(ref)])
    run_command(["samtools", "faidx", str(other)])
    Path(f"{other}.gzi").replace(f"{ref}.gzi")
    with pytest.raises(api.CommandFailed, match="ref.fa.gz.gzi doesn't match"):
        api.ref.verify(RefVerifyParams(ref=ref))


@needs_tools("samtools", "bgzip")
def test_verify_writes_a_missing_index_and_fails_a_stale_dict(tmp_path):
    ref = _bgzipped_fasta(tmp_path, _random_seqs())
    (tmp_path / "ref.dict").write_text(
        "@HD\tVN:1.0\n@SQ\tSN:chr1\tLN:3000\n@SQ\tSN:chr2\tLN:999\n"
    )
    with pytest.raises(api.CommandFailed, match="ref.dict doesn't match"):
        api.ref.verify(RefVerifyParams(ref=ref))
    assert read_fai_lengths(ref) == {"chr1": 3000, "chr2": 1200}
    assert Path(f"{ref}.gzi").is_file()


@needs_tools("samtools")
def test_verify_resolves_a_library_dir_and_matches_the_catalog(
    tmp_path, monkeypatch, caplog
):
    genomes = tmp_path / "genomes"
    genomes.mkdir()
    ref = write_fasta(genomes / "hg38.fa", {"chr1": "ACGT" * 10})
    signature = sequence_signature({"chr1": 40})
    monkeypatch.setattr(
        core_commands,
        "get_available_genomes",
        lambda: [{"final": "hg38.fa", "md5": signature}],
    )

    with caplog.at_level(logging.INFO):
        api.ref.verify(RefVerifyParams(ref=tmp_path))

    assert f"Verifying integrity of {ref}..." in caplog.text
    assert "Sequence dictionary signature: OK" in caplog.text


@needs_tools("samtools")
def test_verify_needs_an_existing_reference(tmp_path):
    with pytest.raises(api.CommandFailed, match="--ref is required"):
        api.ref.verify(RefVerifyParams(ref=None))
    with pytest.raises(api.CommandFailed, match="Reference file not found"):
        api.ref.verify(RefVerifyParams(ref=tmp_path / "missing.fa.gz"))


# --- ref library --list --------------------------------------------------------

GENOMES = [
    {"code": "hg38", "final": "hg38.fa.gz", "label": "hg38 (YSEQ)"},
    {"code": "hs37d5", "final": "hs37d5.fa.gz", "label": "hs37d5 (Dante)"},
    {"code": "T2Tv20", "final": "chm13v2.0.fa.gz", "label": "T2T_v2.0"},
    {"code": "Dog", "final": "UU_Cfam_GSD_1.0.fna.gz", "label": "Dog GSD"},
    {"code": "Cat", "final": "F.catus_Fca126_mat1.0.fna.gz", "label": "Cat Fca126"},
]


@pytest.fixture
def genomes(monkeypatch):
    for module in (core_commands, library_commands):
        monkeypatch.setattr(module, "get_available_genomes", lambda: GENOMES)
    return GENOMES


def _rows(output: str) -> dict[str, list[str]]:
    """Library listing rows keyed by their label column."""
    rows = {}
    for line in output.splitlines():
        fields = [line[41:56], line[57:67], line[68:]]
        fields = [f.strip() for f in fields]
        while fields and not fields[-1]:
            fields.pop()
        if line[:40].strip() and fields:
            rows[line[:40].strip()] = fields
    return rows


def test_library_list_reports_genome_status_and_resources(tmp_path, genomes, capsys):
    gdir, ref_dir = tmp_path / "genomes", tmp_path / "ref"
    gdir.mkdir()
    ref_dir.mkdir()
    (gdir / "hg38.fa.gz").write_bytes(b"x" * 2 * 1024 * 1024)
    (gdir / "hg38.fa.gz.fai").write_text("chr1\t1\n")
    (gdir / "hg38_ncnt.csv").write_text("")
    (gdir / "UU_Cfam_GSD_1.0_ncnt.csv").write_text("")  # bootstrap ships these
    (gdir / "hs37d5.fa.gz.partial").write_bytes(b"x")
    (gdir / "chm13v2.0.fa.gz").write_bytes(b"x")
    (tmp_path / "vep" / "homo_sapiens" / "113_GRCh38").mkdir(parents=True)
    (tmp_path / "vep" / "canis_lupus_familiaris").mkdir(parents=True)
    for name in ("genes_hg19.tsv", "genes_hg38.tsv", "clinvar_hg38.vcf.gz"):
        (ref_dir / name).write_text("")
    (ref_dir / "phylop_hg19.bw").write_text("")
    (ref_dir / "gnomad_hg38.vcf.bgz").write_text("")

    library_commands.cmd_library(Namespace(ref=str(tmp_path), list=True))

    out = capsys.readouterr().out
    assert f"REFERENCE LIBRARY: {tmp_path}" in out
    rows = _rows(out)
    assert rows["hg38 (YSEQ)"] == ["INSTALLED", "2.0 MiB", "N-counts, VEP-GRCh38"]
    assert rows["hs37d5 (Dante)"] == ["INCOMPLETE", "1 B"]  # no GRCh37 cache
    assert rows["T2T_v2.0"] == ["UNINDEXED", "1 B"]
    assert rows["Dog GSD"] == ["MISSING", "", "VEP-Cache"]
    assert rows["Cat Fca126"] == ["MISSING"]  # only the dog's VEP cache is there
    assert rows["Gene Maps (hg19/hg38)"] == ["INSTALLED"]
    assert rows["ClinVar (hg38)"] == ["INSTALLED"]
    assert rows["ClinVar (hg19)"] == ["MISSING"]
    assert rows["PhyloP (hg19)"] == ["INSTALLED"]
    assert rows["gnomAD (hg38)"] == ["INSTALLED"]
    assert rows["gnomAD (hg19)"] == ["MISSING"]
    assert rows["REVEL (hg38)"] == ["MISSING"]


# --- ref library --install / interactive menu -----------------------------------
# Genomes, gene maps and ClinVar install for real from a local HTTP server; only
# the catalogue and the resource URLs are pointed at it.

needs_library_tools = needs_tools(
    "curl", "samtools", "bcftools", "tabix", "bgzip", "htsfile"
)

TINY = {"chr1": "ACGT" * 60 + "N" * 20, "chr2": "GATTACA" * 30}
TINY_LENGTHS = {name: len(seq) for name, seq in TINY.items()}
# The mirrors serve plain gzip; an install recompresses it to BGZF.
TINY_FA_GZ = gzip.compress("".join(f">{n}\n{s}\n" for n, s in TINY.items()).encode())


@pytest.fixture
def server(monkeypatch):
    """Serves server.files; any other path is a 404. Downloads take the one
    non-terminal path even under `pytest -s`, so request counts are stable."""
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    with serving(None) as srv:
        yield srv


@pytest.fixture
def catalog(monkeypatch):
    entries: list[dict[str, str]] = []
    for module in (core_commands, library_commands):
        monkeypatch.setattr(module, "get_available_genomes", lambda: entries)
    return entries


def _genome(server, code, path=None, data=TINY_FA_GZ, final=None):
    """A catalogue entry for the tiny genome, served at path (404 if data is None)."""
    path = path or f"{code}/{code}.fa.gz"
    if data is not None:
        server.files[path] = data
    return {
        "code": code,
        "source": "local",
        "final": final or f"{code}.fa.gz",
        "label": f"{code} (local)",
        "url": server.url_for(path),
        "description": path,
        "md5": sequence_signature(TINY_LENGTHS),
    }


def _install(lib, choice):
    api.ref.library(RefLibraryParams(ref=lib, install=choice))


def _assert_installed(lib, final):
    fasta = lib / "genomes" / final
    assert read_fai_lengths(fasta) == TINY_LENGTHS
    assert Path(f"{fasta}.gzi").is_file()  # recompressed to BGZF
    assert Path(reference_dict_path(fasta)).is_file()


def _files(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in directory.iterdir()}


@needs_library_tools
@pytest.mark.parametrize("choice", ["TINY", "2"])
def test_library_install_by_code_or_number(tmp_path, server, catalog, choice, capsys):
    catalog += [_genome(server, "other"), _genome(server, "tiny")]
    lib = tmp_path / "new-library"

    _install(lib, choice)

    _assert_installed(lib, "tiny.fa.gz")
    assert server.paths == ["tiny/tiny.fa.gz"]
    capsys.readouterr()
    library_commands.cmd_library(Namespace(ref=str(lib), list=True))
    rows = _rows(capsys.readouterr().out)
    assert rows["tiny (local)"][0] == "INSTALLED"
    assert rows["other (local)"] == ["MISSING"]


@needs_library_tools
@pytest.mark.parametrize("choice", ["nope", "0", "3"])
def test_library_install_rejects_unknown_genomes(tmp_path, server, catalog, choice):
    catalog += [_genome(server, "tiny"), _genome(server, "other")]
    with pytest.raises(api.CommandFailed, match=f"Genome '{choice}' not found"):
        _install(tmp_path, choice)
    assert server.requests == []
    assert not (tmp_path / "genomes").exists()


@needs_library_tools
def test_a_genome_unlike_its_catalog_entry_is_not_left_installed(
    tmp_path, server, catalog
):
    """A mirror serving another build or patch level fails its signature check."""
    catalog.append(_genome(server, "tiny") | {"md5": "0" * 32})

    with pytest.raises(api.CommandFailed, match="Failed to install genome tiny.fa.gz"):
        _install(tmp_path, "tiny")

    assert _files(tmp_path / "genomes") == {}


@needs_library_tools
def test_library_install_falls_back_to_the_next_mirror(
    tmp_path, server, catalog, caplog
):
    catalog += [
        _genome(server, "hs38", "down/hs38.fa.gz", data=None),
        _genome(server, "hs38", "wrong/hs38.fa.gz", data=gzip.compress(b">x\nAC\n")),
        _genome(server, "hs38", "good/hs38.fa.gz"),
        _genome(server, "hs38", "spare/hs38.fa.gz"),
    ]
    with caplog.at_level(logging.WARNING):
        _install(tmp_path / "lib", "hs38")

    _assert_installed(tmp_path / "lib", "hs38.fa.gz")
    assert "Trying the next mirror: good/hs38.fa.gz" in caplog.text
    assert "spare/hs38.fa.gz" not in server.paths

    for entry in catalog:
        server.files.pop(entry["description"], None)
    with pytest.raises(api.CommandFailed, match="hs38.fa.gz from any of its 4 mirrors"):
        _install(tmp_path / "empty", "hs38")


@needs_library_tools
def test_a_later_mirror_replaces_a_corrupt_installed_genome(tmp_path, server, catalog):
    """The first mirror only re-indexes an installed genome; when that fails,
    the next one downloads it afresh rather than re-indexing the same file."""
    catalog += [
        _genome(server, "hs38", "first/hs38.fa.gz"),
        _genome(server, "hs38", "second/hs38.fa.gz"),
    ]
    genomes = tmp_path / "genomes"
    genomes.mkdir()
    (genomes / "hs38.fa.gz").write_bytes(TINY_FA_GZ[: len(TINY_FA_GZ) // 2])
    (genomes / "hs38.fa.gz.fai").write_text("chr1\t260\t6\t260\t261\n")

    _install(tmp_path, "hs38")

    _assert_installed(tmp_path, "hs38.fa.gz")
    assert server.paths == ["second/hs38.fa.gz"]


def _menu(monkeypatch, lib, answers):
    replies = iter(answers)

    def fake_input(_prompt=""):
        try:
            return next(replies)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    library_commands.cmd_library(Namespace(ref=str(lib), list=False, install=None))


@pytest.fixture
def menu_library(tmp_path, server, catalog):
    """1) tiny, installed but stale; 2) part, a third downloaded; 3) dog, missing."""
    catalog += [_genome(server, name) for name in ("tiny", "part", "dog")]
    genomes = tmp_path / "genomes"
    genomes.mkdir()
    (genomes / "tiny.fa.gz").write_bytes(b"stale")
    (genomes / "tiny.fa.gz.fai").write_text("chrOld\t5\n")
    partial = genomes / "part.fa.gz.partial"
    partial.write_bytes(TINY_FA_GZ[: len(TINY_FA_GZ) // 3])
    Path(f"{partial}.source").write_text(
        hashlib.sha256(catalog[1]["url"].encode()).hexdigest()
    )
    return genomes


@needs_library_tools
def test_library_menu_shows_each_genomes_status(
    tmp_path, monkeypatch, capsys, menu_library
):
    _menu(monkeypatch, tmp_path, ["0"])

    out = capsys.readouterr().out
    assert " 1) tiny (local) [Installed]" in out
    assert " 2) part (local) [Incomplete]" in out
    assert " 3) dog (local)\n" in out
    assert " C) ClinVar (hg19/hg38)" in out
    assert " P) PhyloP hg38 (~9.9 GB)\n" in out
    assert " K) PharmGKB\n" in out


@needs_library_tools
@pytest.mark.parametrize(
    "answers",
    [["0"], [], ["9"], ["x"], ["1", "n"], ["2", "c"]],
    ids=["exit", "eof", "out-of-range", "not-a-number", "keep", "cancel"],
)
def test_library_menu_choices_that_change_nothing(
    tmp_path, monkeypatch, server, menu_library, answers
):
    before = _files(menu_library)
    _menu(monkeypatch, tmp_path, answers)
    assert _files(menu_library) == before
    assert server.requests == []


@needs_library_tools
def test_library_menu_redownloads_an_installed_genome(
    tmp_path, monkeypatch, menu_library
):
    _menu(monkeypatch, tmp_path, ["1", "y"])
    _assert_installed(tmp_path, "tiny.fa.gz")


@needs_library_tools
def test_library_menu_deletes_an_incomplete_download(
    tmp_path, monkeypatch, server, menu_library
):
    _menu(monkeypatch, tmp_path, ["2", "d"])
    assert sorted(_files(menu_library)) == ["tiny.fa.gz", "tiny.fa.gz.fai"]
    assert server.requests == []


@needs_library_tools
def test_library_menu_resumes_an_incomplete_download(
    tmp_path, monkeypatch, server, menu_library
):
    _menu(monkeypatch, tmp_path, ["2", "r"])
    _assert_installed(tmp_path, "part.fa.gz")
    assert server.gets() == [f"bytes={len(TINY_FA_GZ) // 3}-"]


@needs_library_tools
def test_library_menu_installs_a_missing_genome(tmp_path, monkeypatch, menu_library):
    _menu(monkeypatch, tmp_path, ["3"])
    _assert_installed(tmp_path, "dog.fa.gz")


# --- ref gene-map / resource downloads / bootstrap ------------------------------

REFGENE_GZ = gzip.compress(
    b"0\tNM_007294\tchr17\t-\t43044294\t43125364\t0\t0\t0\t0\t0\t0\tBRCA1\tcmpl\tcmpl\t0,\n"
)


@pytest.fixture
def ucsc(monkeypatch, server):
    for build in GENE_MAP_URLS:
        server.files[f"{build}/refGene.txt.gz"] = REFGENE_GZ
        monkeypatch.setitem(
            GENE_MAP_URLS, build, server.url_for(f"{build}/refGene.txt.gz")
        )
    return server


def _gene_maps(lib: Path) -> list[str]:
    return sorted(p.name for p in (lib / "ref").glob("genes_*.tsv"))


def test_gene_map_command_installs_and_deletes_both_builds(tmp_path, ucsc, capsys):
    # PR #458 review: the success message went to stdout through the API too.
    api.ref.gene_map(RefGeneMapParams(ref=tmp_path, delete=False))
    assert capsys.readouterr().out == ""
    assert _gene_maps(tmp_path) == ["genes_hg19.tsv", "genes_hg38.tsv"]
    assert (
        "BRCA1\tchr17\t43044295\t43125364"
        in (tmp_path / "ref" / "genes_hg38.tsv").read_text()
    )

    with patch.object(
        sys,
        "argv",
        ["wgsextract", "ref", "gene-map", "--ref", str(tmp_path), "--delete"],
    ):
        main()
    assert "Successfully deleted Gene Maps." in capsys.readouterr().out
    assert _gene_maps(tmp_path) == []


def test_a_fasta_ref_names_the_library_above_its_genomes_folder(tmp_path, ucsc):
    # PR #458 review: `--ref <library>/genomes/hg38.fa` (the configured
    # reference) treated the FASTA itself as the library folder.
    fasta = tmp_path / "genomes" / "hg38.fa"
    fasta.parent.mkdir()
    fasta.write_text(">chr1\nA\n")
    api.ref.gene_map(RefGeneMapParams(ref=fasta, delete=False))
    assert _gene_maps(tmp_path) == ["genes_hg19.tsv", "genes_hg38.tsv"]


def test_gene_map_command_reports_failures(tmp_path, ucsc):
    del ucsc.files["hg19/refGene.txt.gz"]
    with pytest.raises(
        api.CommandFailed, match="Gene Map download cancelled or failed"
    ):
        api.ref.gene_map(RefGeneMapParams(ref=tmp_path, delete=False))

    (tmp_path / "ref" / "genes_hg19.tsv").mkdir()  # can't be unlinked
    with pytest.raises(api.CommandFailed, match="Failed to delete Gene Maps"):
        api.ref.gene_map(RefGeneMapParams(ref=tmp_path, delete=True))


@needs_library_tools
@pytest.mark.parametrize(
    ("installed", "answers", "expected"),
    [
        (False, ["g"], ["genes_hg19.tsv", "genes_hg38.tsv"]),
        (True, ["g", "y"], []),
        (True, ["g", "n"], ["genes_hg19.tsv", "genes_hg38.tsv"]),
    ],
    ids=["install", "delete", "keep"],
)
def test_library_menu_gene_maps(
    tmp_path, monkeypatch, catalog, ucsc, installed, answers, expected
):
    if installed:
        api.ref.gene_map(RefGeneMapParams(ref=tmp_path, delete=False))
    ucsc.requests.clear()
    _menu(monkeypatch, tmp_path, answers)
    assert _gene_maps(tmp_path) == expected
    assert bool(ucsc.requests) == (not installed)


VCF = (
    "##fileformat=VCFv4.2\n##contig=<ID=1,length=1000>\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
    "1\t100\t12345\tA\tG\t.\t.\tCLNSIG=Pathogenic\n"
)
REVEL_TSV = "#Chr\tStart\tEnd\tRef\tAlt\tREVEL\n1\t100\t100\tA\tG\t0.5\n"
ALPHAMISSENSE_TSV = (
    "#CHROM\tPOS\tREF\tALT\tgenome\tuniprot_id\ttranscript_id\tprotein_variant\t"
    "am_pathogenicity\tam_class\n"
    "1\t100\tA\tG\thg38\tP1\tT1\tV1A\t0.9\tlikely_pathogenic\n"
)


def _bgzf(tmp_path: Path, text: str, index: bool) -> dict[str, bytes]:
    """A BGZF VCF as served, and with index its published .tbi."""
    vcf = tmp_path / "served.vcf"
    vcf.write_text(text)
    served = Path(bgzip_index(vcf))
    files = {"": served.read_bytes()}
    if index:
        files[".tbi"] = Path(f"{served}.tbi").read_bytes()
    return files


def _resource(name: str, **fields):
    params = getattr(api, f"Ref{name.capitalize()}Params")
    return getattr(api.ref, name)(params(**fields))


@needs_tools("bgzip", "tabix", "bcftools")
@pytest.mark.parametrize(
    ("command", "extra", "urls", "dest", "payload"),
    [
        ("clinvar", {}, CLINVAR_URLS, "clinvar_{}.vcf.gz", "vcf-gzip"),
        ("revel", {}, REVEL_URLS, "revel_{}.tsv.gz", REVEL_TSV),
        (
            "alphamissense",
            {},
            ALPHAMISSENSE_URLS,
            "alphamissense_{}.tsv.gz",
            ALPHAMISSENSE_TSV,
        ),
        ("spliceai", {}, SPLICEAI_URLS, "spliceai_{}.vcf.gz", "vcf-indexed"),
        (
            "gnomad",
            {"release": "2.1.1"},
            GNOMAD_URLS,
            "gnomad_{}.vcf.bgz",
            "vcf-bgzf",
        ),
    ],
    ids=["clinvar", "revel", "alphamissense", "spliceai", "gnomad-2.1.1"],
)
def test_resource_commands_install_indexed_files(
    tmp_path, monkeypatch, server, command, extra, urls, dest, payload
):
    if payload == "vcf-gzip":
        files = {"": gzip.compress(VCF.encode())}
    elif payload.startswith("vcf-"):
        files = _bgzf(tmp_path, VCF, index=payload == "vcf-indexed")
    else:
        files = {"": gzip.compress(payload.encode())}
    builds = [build for build, url in urls.items() if url]  # SpliceAI hg19: manual
    resources = (CLINVAR_URLS, REVEL_URLS, ALPHAMISSENSE_URLS, SPLICEAI_URLS)
    for other in (*resources, GNOMAD_URLS):
        for build, url in other.items():
            if url:  # Any other resource is a 404, never the real network.
                monkeypatch.setitem(other, build, server.url_for("unserved"))
    monkeypatch.setattr(gnomad_af, "GNOMAD_V4_URL", server.url_for("{contig}"))
    for build in builds:
        for suffix, data in files.items():
            server.files[f"{build}/source{suffix}"] = data
        monkeypatch.setitem(urls, build, server.url_for(f"{build}/source"))
    lib = tmp_path / "library"

    _resource(command, ref=lib, **extra)

    tabix = get_tool_path("tabix")
    for build in builds:
        path = str(lib / "ref" / dest.format(build))
        listed = run_command([tabix, "-l", path], capture_output=True).stdout
        assert listed.split() == ["1"]


@needs_library_tools
def test_library_menu_installs_clinvar(tmp_path, monkeypatch, server, catalog):
    for build in CLINVAR_URLS:
        server.files[f"{build}/clinvar.vcf.gz"] = gzip.compress(VCF.encode())
        monkeypatch.setitem(
            CLINVAR_URLS, build, server.url_for(f"{build}/clinvar.vcf.gz")
        )

    _menu(monkeypatch, tmp_path, ["c"])

    for build in ("hg19", "hg38"):
        vcf = tmp_path / "ref" / f"clinvar_{build}.vcf.gz"
        assert vcf_calls(vcf) == [("1", "100", "A", "G")]


@needs_tools("bgzip", "tabix", "bcftools")
def test_pharmgkb_command_builds_its_table(tmp_path, monkeypatch, server):
    def archive(member: str, text: str) -> bytes:
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as zf:
            zf.writestr(member, text)
        return data.getvalue()

    server.files["clinical.zip"] = archive(
        "clinical_annotations.tsv",
        "Clinical Annotation ID\tVariant/Haplotypes\tGene\tLevel of Evidence\t"
        "Phenotype Category\tDrug(s)\n1\trs200\tVKORC1\t1A\tDosage\twarfarin\n",
    )
    server.files["variants.zip"] = archive(
        "variants.tsv",
        "Variant ID\tVariant Name\tGene IDs\tGene Symbols\tLocation\tSynonyms\n"
        "PA1\trs200\tPA8\tVKORC1\tNC_000016.10:31096368\t"
        "NC_000016.10:g.31096368C>T\n",
    )
    monkeypatch.setattr(pharmgkb, "CLINICAL_URL", server.url_for("clinical.zip"))
    monkeypatch.setattr(pharmgkb, "VARIANTS_URL", server.url_for("variants.zip"))

    api.ref.pharmgkb(api.RefPharmgkbParams(ref=tmp_path))

    with gzip.open(tmp_path / "ref" / "pharmgkb_hg38.tsv.gz", "rt") as f:
        assert "chr16\t31096368\tC\tT\trs200|VKORC1|1A|Dosage|warfarin" in f.read()


@needs_tools("bgzip", "tabix")
def test_resource_download_failure_names_the_resource(tmp_path, monkeypatch, server):
    for build in CLINVAR_URLS:
        monkeypatch.setitem(CLINVAR_URLS, build, server.url_for(build))
    with pytest.raises(api.CommandFailed, match="ClinVar setup failed"):
        api.ref.clinvar(api.RefClinvarParams(ref=tmp_path))


def _cadd_hg19() -> list[str]:
    return [
        f"{name}_hg19.tsv.gz{ext}"
        for name in ("cadd", "cadd_indel")
        for ext in ("", ".tbi")
    ]


@pytest.mark.parametrize(
    ("command", "extra", "installed"),
    [
        ("phylop", {"build": "hg19"}, ["phylop_hg19.bw"]),
        ("gerp", {"build": "hg19"}, ["gerp_hg19.bw"]),
        ("cadd", {"build": "hg19"}, _cadd_hg19()),
        (
            "gnomad",
            {"release": "4.1"},
            ["gnomad_hg38.tsv.gz", "gnomad_hg38.tsv.gz.tbi"],
        ),
    ],
    ids=["phylop", "gerp", "cadd", "gnomad-4.1"],
)
def test_installed_resources_are_found_for_the_requested_build_or_release(
    tmp_path, monkeypatch, server, command, extra, installed
):
    """These are already in the library; anything else would be fetched, and
    fail, from the empty server."""
    for urls in (PHYLOP_URLS, GERP_URLS, GNOMAD_URLS):
        for build in urls:
            monkeypatch.setitem(urls, build, server.url_for(build))
    monkeypatch.setattr(annotation_resources, "CADD_BASE_URL", server.base)
    monkeypatch.setattr(gnomad_af, "GNOMAD_V4_URL", server.url_for("{contig}"))
    (tmp_path / "ref").mkdir()
    for file in installed:
        (tmp_path / "ref" / file).write_bytes(b"installed")

    _resource(command, ref=tmp_path, **extra)

    assert server.requests == []


def test_failed_bootstrap_does_not_save_the_library(tmp_path, monkeypatch, server):
    monkeypatch.setattr(constants, "BOOTSTRAP_URL", server.url_for("bootstrap.tar.gz"))
    reflib = tmp_path / "library"

    with pytest.raises(api.CommandFailed, match="bootstrap failed"):
        api.ref.bootstrap(api.RefBootstrapParams(ref=reflib))

    assert len(server.gets()) == 3  # retried
    config.reload_settings()
    assert not config.settings.get("reference_library")
    assert not (reflib / constants.BOOTSTRAP_FILENAME).exists()


def test_library_list_finds_the_build_of_a_genome_from_its_signature(
    tmp_path, monkeypatch, capsys
):
    """human_g1k_v37's name has no build in it; its catalog signature does."""
    g1k = {
        "code": "hs37-",
        "final": "human_g1k_v37.fasta.gz",
        "label": "human_g1k_v37",
        "md5": "88b5f4bbaaec4a097b3aa160bf776093",
    }
    monkeypatch.setattr(core_commands, "get_available_genomes", lambda: [g1k])
    (tmp_path / "genomes").mkdir()
    (tmp_path / "genomes" / g1k["final"]).write_bytes(b"x" * 30_000)
    (tmp_path / "vep" / "homo_sapiens" / "113_GRCh37").mkdir(parents=True)

    library_commands.cmd_library(Namespace(ref=str(tmp_path), list=True))

    row = _rows(capsys.readouterr().out)["human_g1k_v37"]
    assert row[1:] == ["29.3 KiB", "VEP-GRCh37"]
