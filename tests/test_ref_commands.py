"""`wgsextract ref` index/count-ns/verify/library commands on tiny references."""

import gzip
import logging
import random
from argparse import Namespace
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools, write_fasta
from wgsextract_cli.commands.ref import core_commands, library_commands
from wgsextract_cli.core import bwa_mem2, config
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.reference_assets import read_fai_lengths, sequence_signature
from wgsextract_cli.core.utils import WGSExtractError, run_command


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

    core_commands.cmd_index(Namespace(ref=str(ref), aligner="bwa"))

    assert read_fai_lengths(ref) == {"chr1": 3000, "chr2": 1200}
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

    core_commands.cmd_index(Namespace(ref=str(ref), aligner="bwa-mem2"))

    assert (tmp_path / "ref.dict").is_file()
    assert bwa_mem2.has_index(str(ref))
    assert not (tmp_path / "ref.fa.gz.sa").exists()  # bwa's index wasn't built


@needs_tools("samtools", "bgzip", "bwa-mem2")
def test_index_bwa_mem2_without_memory_fails_after_the_fai_and_dict(
    tmp_path, monkeypatch
):
    ref = _bgzipped_fasta(tmp_path, _random_seqs())
    monkeypatch.setattr(bwa_mem2, "_total_memory", lambda: 0)

    with pytest.raises(WGSExtractError, match="Indexing failed: .*--aligner bwa"):
        core_commands.cmd_index(Namespace(ref=str(ref), aligner="bwa-mem2"))

    assert (tmp_path / "ref.fa.gz.fai").is_file()
    assert (tmp_path / "ref.dict").is_file()
    assert not bwa_mem2.has_index(str(ref))


@needs_tools("samtools", "bwa")
def test_index_of_plain_gzip_fasta_reports_the_samtools_failure(tmp_path):
    ref = tmp_path / "ref.fa.gz"
    with gzip.open(ref, "wt") as f:  # gzip, not bgzip: samtools can't index it
        f.write(">chr1\nACGTACGT\n")

    with pytest.raises(WGSExtractError, match="Indexing failed"):
        core_commands.cmd_index(Namespace(ref=str(ref), aligner="bwa"))
    assert not (tmp_path / "ref.fa.gz.bwt").exists()


@needs_tools("samtools", "bwa")
@pytest.mark.parametrize(
    ("ref", "message"), [(None, "--ref is required"), ("nope.fa", "file not found")]
)
def test_index_needs_an_existing_ref(tmp_path, ref, message):
    ref = ref and str(tmp_path / ref)
    with pytest.raises(WGSExtractError, match=message):
        core_commands.cmd_index(Namespace(ref=ref, aligner=None))


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
    with pytest.raises(WGSExtractError, match="N-counting failed: .*Pass --outdir"):
        core_commands.cmd_count_ns(Namespace(ref=str(ref), outdir=None))


# --- ref verify --------------------------------------------------------------


@needs_tools("samtools", "bgzip", "gzip")
def test_verify_passes_an_indexed_bgzipped_fasta(tmp_path, caplog):
    seqs = _random_seqs()
    ref = _bgzipped_fasta(tmp_path, seqs)
    run_command(["samtools", "faidx", str(ref)])
    run_command(["samtools", "dict", str(ref), "-o", str(tmp_path / "ref.dict")])

    with caplog.at_level(logging.INFO):
        core_commands.cmd_ref_verify(Namespace(ref=str(ref)))

    signature = sequence_signature({n: len(s) for n, s in seqs.items()})
    assert f"Sequence dictionary signature: {signature}" in caplog.text
    assert "Reference ref.fa.gz appears to be valid." in caplog.text
    assert "No sequence dictionary" not in caplog.text


@needs_tools("samtools", "bgzip", "gzip")
def test_verify_fails_a_truncated_gzip(tmp_path):
    ref = _bgzipped_fasta(tmp_path, _random_seqs())
    data = ref.read_bytes()
    ref.write_bytes(data[: len(data) // 2])
    with pytest.raises(WGSExtractError, match="Gzip integrity check failed"):
        core_commands.cmd_ref_verify(Namespace(ref=str(ref)))


@needs_tools("samtools", "gzip")
def test_verify_indexes_a_plain_fasta_and_warns_without_a_dict(tmp_path, caplog):
    ref = tmp_path / "ref.fa"
    ref.write_text(">chrM\nACGTNNACGT\n")

    with caplog.at_level(logging.INFO):
        core_commands.cmd_ref_verify(Namespace(ref=str(ref)))

    assert read_fai_lengths(ref) == {"chrM": 10}
    assert "Gzip" not in caplog.text
    assert f"No sequence dictionary {tmp_path / 'ref.dict'}" in caplog.text


@needs_tools("samtools", "gzip")
def test_verify_fails_an_fai_pointing_past_the_end_of_the_fasta(tmp_path):
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": "ACGT" * 10})
    Path(f"{ref}.fai").write_text("chr1\t40\t100000\t40\t41\n")
    with pytest.raises(WGSExtractError, match="Samtools faidx check failed"):
        core_commands.cmd_ref_verify(Namespace(ref=str(ref)))


@needs_tools("samtools", "gzip")
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
        core_commands.cmd_ref_verify(Namespace(ref=str(tmp_path)))

    assert f"Verifying integrity of {ref}..." in caplog.text
    assert "Sequence dictionary signature: OK" in caplog.text


@needs_tools("samtools", "gzip")
def test_verify_needs_an_existing_reference(tmp_path):
    with pytest.raises(WGSExtractError, match="--ref is required"):
        core_commands.cmd_ref_verify(Namespace(ref=None))
    with pytest.raises(WGSExtractError, match="Reference file not found"):
        core_commands.cmd_ref_verify(Namespace(ref=str(tmp_path / "missing.fa.gz")))


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
    assert rows["hg38 (YSEQ)"] == ["INSTALLED", "2.0 MB", "N-counts, VEP-GRCh38"]
    assert rows["hs37d5 (Dante)"] == ["INCOMPLETE", "0.0 MB"]  # no GRCh37 cache
    assert rows["T2T_v2.0"] == ["UNINDEXED", "0.0 MB"]
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


@pytest.fixture
def installs(monkeypatch, genomes):
    """Record genome installs and deletions instead of downloading."""
    calls: list[tuple] = []
    monkeypatch.setattr(library_commands, "verify_dependencies", lambda deps: None)
    monkeypatch.setattr(library_commands, "log_dependency_info", lambda deps: None)
    monkeypatch.setattr(
        library_commands,
        "download_and_process_genome",
        lambda g, lib, restart: calls.append(("install", g["code"], restart)) or True,
    )
    monkeypatch.setattr(
        library_commands,
        "delete_genome",
        lambda final, lib: calls.append(("delete", final)),
    )
    return calls


@pytest.mark.parametrize("choice", ["t2tv20", "3"])
def test_library_install_by_code_or_number(tmp_path, installs, choice):
    reflib = tmp_path / "new-library"
    library_commands.cmd_library(Namespace(ref=str(reflib), list=False, install=choice))
    assert installs == [("install", "T2Tv20", False)]
    assert reflib.is_dir()


@pytest.mark.parametrize("choice", ["nope", "0", "6"])
def test_library_install_rejects_unknown_genomes(tmp_path, installs, choice):
    with pytest.raises(WGSExtractError, match=f"Genome '{choice}' not found"):
        library_commands.cmd_library(
            Namespace(ref=str(tmp_path), list=False, install=choice)
        )
    assert installs == []


def test_library_install_failure_names_the_genome(tmp_path, installs, monkeypatch):
    monkeypatch.setattr(
        library_commands, "download_and_process_genome", lambda *a, **k: False
    )
    with pytest.raises(WGSExtractError, match="Failed to install genome hg38.fa.gz"):
        library_commands.cmd_library(
            Namespace(ref=str(tmp_path), list=False, install="hg38")
        )


def _menu(monkeypatch, tmp_path, answers):
    replies = iter(answers)

    def fake_input(_prompt=""):
        try:
            return next(replies)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    library_commands.cmd_library(Namespace(ref=str(tmp_path), list=False, install=None))


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (["0"], []),
        ([], []),  # EOF
        (["1", "n"], []),  # installed: keep it
        (["1", "y"], [("install", "hg38", True)]),
        (["2", "c"], []),  # incomplete: cancel
        (["2", "d"], [("delete", "hs37d5.fa.gz")]),
        (["2", "r"], [("install", "hs37d5", False)]),
        (["4"], [("install", "Dog", False)]),
        (["9"], []),
        (["x"], []),
    ],
)
def test_library_menu_genome_choices(
    tmp_path, monkeypatch, capsys, installs, answers, expected
):
    gdir = tmp_path / "genomes"
    gdir.mkdir()
    for name in ("hg38.fa.gz", "hg38.fa.gz.fai", "hs37d5.fa.gz.partial"):
        (gdir / name).write_text("")

    _menu(monkeypatch, tmp_path, answers)

    assert installs == expected
    out = capsys.readouterr().out
    assert " 1) hg38 (YSEQ) [Installed]" in out
    assert " 2) hs37d5 (Dante) [Incomplete]" in out
    assert " 4) Dog GSD\n" in out


def test_library_menu_lists_and_runs_resource_downloads(
    tmp_path, monkeypatch, capsys, installs
):
    reflibs = []
    monkeypatch.setitem(
        library_commands._MENU_DOWNLOADS,
        "C",
        ("ClinVar", lambda lib: reflibs.append(lib) or True),
    )
    _menu(monkeypatch, tmp_path, ["c"])

    assert reflibs == [str(tmp_path)]
    out = capsys.readouterr().out
    assert " C) ClinVar (hg19/hg38)" in out
    assert " P) PhyloP hg38 (~9.9 GB)\n" in out
    assert " K) PharmGKB\n" in out


@pytest.mark.parametrize(
    ("installed", "answers", "expected"),
    [
        (False, ["g"], ["download"]),
        (True, ["g", "y"], ["delete"]),
        (True, ["g", "n"], []),
    ],
)
def test_library_menu_gene_maps(
    tmp_path, monkeypatch, installs, installed, answers, expected
):
    calls = []
    monkeypatch.setattr(
        library_commands, "are_gene_maps_installed", lambda lib: installed
    )
    monkeypatch.setattr(
        library_commands,
        "download_gene_maps",
        lambda lib: calls.append("download") or True,
    )
    monkeypatch.setattr(
        library_commands, "delete_gene_maps", lambda lib: calls.append("delete") or True
    )
    _menu(monkeypatch, tmp_path, answers)
    assert calls == expected


# --- ref gene-map / resource downloads / bootstrap ------------------------------


@pytest.mark.parametrize(
    ("delete", "func", "ok", "message"),
    [
        (False, "download_gene_maps", True, "Successfully downloaded Gene Maps."),
        (False, "download_gene_maps", False, "Gene Map download cancelled or failed."),
        (True, "delete_gene_maps", True, "Successfully deleted Gene Maps."),
        (True, "delete_gene_maps", False, "Failed to delete Gene Maps."),
    ],
)
def test_gene_map_download_and_delete(
    tmp_path, monkeypatch, capsys, delete, func, ok, message
):
    reflibs = []
    monkeypatch.setattr(library_commands, func, lambda lib: reflibs.append(lib) or ok)
    args = Namespace(ref=str(tmp_path), delete=delete)
    if ok:
        library_commands.cmd_gene_map(args)
        assert message in capsys.readouterr().out
    else:
        with pytest.raises(WGSExtractError, match=message):
            library_commands.cmd_gene_map(args)
    assert reflibs == [str(tmp_path)]


@pytest.mark.parametrize(
    ("command", "extra", "downloader", "expected_args"),
    [
        ("cmd_clinvar_dl", {}, "download_clinvar", ()),
        ("cmd_revel_dl", {}, "download_revel", ()),
        ("cmd_phylop_dl", {"build": "hg19"}, "download_phylop", ("hg19",)),
        ("cmd_gnomad_dl", {"release": "2.1.1"}, "download_gnomad", ()),
        ("cmd_gnomad_dl", {"release": "4.1"}, "build_gnomad_af", (6,)),
        (
            "cmd_gnomad_dl",
            {"release": "4.1", "threads": "3"},
            "build_gnomad_af",
            (3,),
        ),
        ("cmd_spliceai_dl", {}, "download_spliceai", ()),
        ("cmd_alphamissense_dl", {}, "download_alphamissense", ()),
        ("cmd_pharmgkb_dl", {}, "download_pharmgkb", ()),
        ("cmd_cadd_dl", {"build": "hg38"}, "download_cadd", ("hg38",)),
        ("cmd_gerp_dl", {"build": "hg38"}, "download_gerp", ("hg38",)),
    ],
)
def test_resource_downloads_call_their_downloader(
    tmp_path, monkeypatch, command, extra, downloader, expected_args
):
    calls = []
    monkeypatch.setattr(
        library_commands, downloader, lambda *a: calls.append(a) or True
    )
    getattr(library_commands, command)(Namespace(ref=str(tmp_path), **extra))
    assert calls == [(str(tmp_path), *expected_args)]


def test_resource_download_failure_names_the_resource(tmp_path, monkeypatch):
    monkeypatch.setattr(library_commands, "download_cadd", lambda lib, build: False)
    with pytest.raises(WGSExtractError, match="CADD hg19 setup failed"):
        library_commands.cmd_cadd_dl(Namespace(ref=str(tmp_path), build="hg19"))


def test_failed_bootstrap_does_not_save_the_library(tmp_path, monkeypatch):
    saved = []
    monkeypatch.setattr(config, "save_config", saved.append)
    monkeypatch.setattr(
        "wgsextract_cli.core.reference_processing.download_bootstrap", lambda p: False
    )
    with pytest.raises(WGSExtractError, match="bootstrap failed"):
        library_commands.cmd_bootstrap(Namespace(ref=str(tmp_path)))
    assert saved == []
