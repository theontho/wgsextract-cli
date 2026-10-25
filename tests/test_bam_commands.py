"""`bam` sort/index/unindex/unsort/to-cram/to-bam/unalign/identify/merge on a
tiny BAM: the exact reads, flags and headers each one writes, the reference a CRAM
is made with, and what is left behind when a step fails."""

import hashlib
import logging
import random
import shutil
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_htslib, run_wgsextract, sam_to_bam, write_fasta
from wgsextract_cli.commands import bam as bam_module
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.samtools_commands import _use_sambamba
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import calculate_bam_md5, get_file_version

pytestmark = needs_htslib
# sambamba sorts and indexes BAMs where installed (Linux); samtools elsewhere.
with_sambamba = pytest.mark.skipif(not _use_sambamba(), reason="samtools sorts here")

LENGTHS = {"chr1": 5000, "chr2": 3000}
QUAL = "I" * 50
COMP = str.maketrans("ACGT", "TGCA")


def _revcomp(seq: str) -> str:
    return seq.translate(COMP)[::-1]


@pytest.fixture(scope="module")
def genome(tmp_path_factory):
    """A reference and a coordinate-sorted, indexed BAM (read group L1) holding
    proper pairs p0-p2, p3 whose second mate is unmapped, an unmapped pair u0, a
    single-end read s0, and a secondary and a supplementary copy of p0/1."""
    root = tmp_path_factory.mktemp("bam")
    rng = random.Random(11)
    seqs = {
        c: "".join(rng.choice("ACGT") for _ in range(n)) for c, n in LENGTHS.items()
    }
    ref = write_fasta(root / "ref.fa", seqs)

    def at(contig: str, pos: int) -> str:
        return seqs[contig][pos - 1 : pos + 49]

    rg = "RG:Z:L1"
    lines = [
        "@HD\tVN:1.6\tSO:coordinate",
        *(f"@SQ\tSN:{c}\tLN:{n}" for c, n in LENGTHS.items()),
        "@RG\tID:L1\tSM:S1\tPL:ILLUMINA",
    ]
    for name, contig, pos in (
        ("p0", "chr1", 100),
        ("p1", "chr1", 1000),
        ("p2", "chr2", 500),
    ):
        mate = pos + 200
        lines += [
            f"{name}\t99\t{contig}\t{pos}\t60\t50M\t=\t{mate}\t250\t{at(contig, pos)}\t{QUAL}\t{rg}",
            f"{name}\t147\t{contig}\t{mate}\t60\t50M\t=\t{pos}\t-250\t{at(contig, mate)}\t{QUAL}\t{rg}",
        ]  # fmt: skip
    lines += [
        f"p0\t355\tchr2\t100\t0\t50M\t=\t700\t0\t{at('chr2', 100)}\t{QUAL}\t{rg}",
        f"p0\t2147\tchr2\t900\t20\t20M30H\tchr1\t300\t0\t{at('chr2', 900)[:20]}\t{QUAL[:20]}\t{rg}",
        f"p3\t73\tchr2\t2000\t60\t50M\t=\t2000\t0\t{at('chr2', 2000)}\t{QUAL}\t{rg}",
        f"p3\t133\tchr2\t2000\t0\t*\t=\t2000\t0\t{'A' * 50}\t{QUAL}\t{rg}",
        f"s0\t0\tchr1\t3000\t60\t50M\t*\t0\t0\t{at('chr1', 3000)}\t{QUAL}\t{rg}",
        f"u0\t77\t*\t0\t0\t*\t*\t0\t0\t{'C' * 50}\t{QUAL}\t{rg}",
        f"u0\t141\t*\t0\t0\t*\t*\t0\t0\t{'G' * 50}\t{QUAL}\t{rg}",
    ]  # fmt: skip
    bam = sam_to_bam(lines, root / "s.bam")
    return {"ref": str(ref), "seqs": seqs, "bam": bam, "root": root}


def _samtools(*args: str) -> str:
    return run_command(["samtools", *args], capture_output=True).stdout


def _records(path, ref: str | None = None) -> list[tuple[str, ...]]:
    """QNAME, FLAG, RNAME, POS, CIGAR, SEQ and RG of each record, in file order."""
    out = _samtools("view", *(["-T", ref] if ref else []), str(path))
    rows: list[tuple[str, ...]] = []
    for line in out.splitlines():
        f = line.split("\t")
        rg = next((t for t in f[11:] if t.startswith("RG:")), "")
        rows.append((f[0], f[1], f[2], f[3], f[5], f[9], rg))
    return rows


def _header(path) -> list[str]:
    """Header lines other than @PG, which every samtools step appends to."""
    return [
        h for h in _samtools("view", "-H", str(path)).splitlines() if h[:3] != "@PG"
    ]


def _copy(genome, tmp_path: Path, *with_index: str) -> Path:
    """The fixture BAM (and the named index suffixes) copied into tmp_path."""
    dst = tmp_path / "s.bam"
    shutil.copy(genome["bam"], dst)
    for suffix in with_index:
        shutil.copy(genome["bam"] + suffix, str(dst) + suffix)
    return dst


def _cram(genome, tmp_path: Path) -> Path:
    cram = tmp_path / "s.cram"
    run_command(
        ["samtools", "view", "-C", "-T", genome["ref"], "-o", str(cram), genome["bam"]]
    )
    run_command(["samtools", "index", str(cram)])
    return cram


@pytest.fixture
def no_remote_refs(tmp_path, monkeypatch):
    """Keep htslib from fetching a CRAM's reference by its M5 from the EBI server."""
    monkeypatch.setenv("REF_PATH", str(tmp_path / "no-ref-cache"))
    monkeypatch.setenv("REF_CACHE", str(tmp_path / "no-ref-cache"))


def _bam(*argv: str) -> None:
    run_wgsextract("bam", *argv, "--threads", "1", "--memory", "100M")


# --- sort -----------------------------------------------------------------


def test_sort_coordinate_sorts_a_name_sorted_bam(genome, tmp_path):
    unsorted = tmp_path / "n.bam"
    run_command(["samtools", "sort", "-n", "-o", str(unsorted), genome["bam"]])
    before = unsorted.read_bytes()
    _bam("sort", "--input", str(unsorted), "--outdir", str(tmp_path / "out"))

    out = tmp_path / "out" / "n_sorted.bam"
    assert _records(out) == _records(genome["bam"])
    assert "@HD\tVN:1.6\tSO:coordinate" in _header(out)
    assert unsorted.read_bytes() == before
    # No partial output or sort temp directory is left behind.
    assert sorted(p.name for p in out.parent.iterdir() if p.suffix != ".bai") == [
        "n_sorted.bam"
    ]


def test_sort_region_keeps_only_that_contig(genome, tmp_path):
    _bam("sort", "--input", genome["bam"], "--outdir", str(tmp_path), "-r", "chr2")
    records = _records(tmp_path / "s_sorted.bam")
    assert {r[2] for r in records} == {"chr2"}
    assert [r[0] for r in records] == ["p0", "p2", "p2", "p0", "p3", "p3"]


def test_sort_cram_writes_cram_against_the_reference(genome, tmp_path):
    cram = _cram(genome, tmp_path)
    out_dir = tmp_path / "out"
    _bam("sort", "--input", str(cram), "--ref", genome["ref"], "--outdir", str(out_dir))

    out = out_dir / "s_sorted.cram"
    assert get_file_version(out).startswith("CRAM")
    assert _records(out, genome["ref"]) == _records(genome["bam"])


@with_sambamba
def test_failed_sort_leaves_no_partial_output(genome, tmp_path):
    broken = tmp_path / "broken.bam"
    broken.write_bytes(Path(genome["bam"]).read_bytes()[:300])  # truncated BGZF
    out_dir = tmp_path / "out"
    with pytest.raises(WGSExtractError):
        _bam("sort", "--input", str(broken), "--outdir", str(out_dir))
    assert list(out_dir.iterdir()) == []


def test_sort_refuses_when_outdir_lacks_space(genome, tmp_path, monkeypatch):
    monkeypatch.setattr(bam_module, "check_free_space", lambda _dir, _gb: False)
    with pytest.raises(WGSExtractError, match="Insufficient free disk space"):
        _bam("sort", "--input", genome["bam"], "--outdir", str(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("cmd", ["index", "identify", "unindex"])
def test_commands_need_an_input(cmd):
    with pytest.raises(WGSExtractError, match="input"):
        _bam(cmd)


# --- index / unindex ------------------------------------------------------


def test_index_writes_a_usable_bai(genome, tmp_path):
    bam = _copy(genome, tmp_path)
    _bam("index", "--input", str(bam))
    assert Path(f"{bam}.bai").is_file()
    # A region query needs the index.
    assert {r[0] for r in _records_region(bam, "chr1:900-1300")} == {"p1"}


def _records_region(path, region: str) -> list[list[str]]:
    out = _samtools("view", str(path), region)
    return [line.split("\t") for line in out.splitlines()]


def test_index_writes_crai_for_cram(genome, tmp_path, no_remote_refs):
    cram = _cram(genome, tmp_path)
    Path(f"{cram}.crai").unlink()
    _bam("index", "--input", str(cram))
    assert Path(f"{cram}.crai").is_file()
    assert not Path(f"{cram}.bai").exists()


@with_sambamba
def test_index_of_unsorted_bam_fails_without_leaving_an_index(genome, tmp_path):
    unsorted = tmp_path / "n.bam"
    run_command(["samtools", "sort", "-n", "-o", str(unsorted), genome["bam"]])
    with pytest.raises(WGSExtractError, match="Indexing failed"):
        _bam("index", "--input", str(unsorted))
    assert not Path(f"{unsorted}.bai").exists()


def test_unindex_removes_bai_and_crai_only(genome, tmp_path):
    bam = _copy(genome, tmp_path, ".bai")
    Path(f"{bam}.crai").write_bytes(b"")
    before = bam.read_bytes()
    _bam("unindex", "--input", str(bam))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.bam"]
    assert bam.read_bytes() == before


@pytest.mark.parametrize("index_name", ["s.bai", "s.bam.csi"])
def test_unindex_removes_every_index_samtools_would_use(genome, tmp_path, index_name):
    bam = _copy(genome, tmp_path)
    if index_name.endswith(".csi"):
        run_command(["samtools", "index", "-c", str(bam)])
    else:
        run_command(["samtools", "index", str(bam), str(tmp_path / index_name)])
    _bam("unindex", "--input", str(bam))
    assert not (tmp_path / index_name).exists()


# --- unsort ---------------------------------------------------------------


def test_unsort_only_relabels_the_header(genome, tmp_path):
    _bam("unsort", "--input", genome["bam"], "--outdir", str(tmp_path))
    out = tmp_path / "s_unsorted.bam"
    assert _records(out) == _records(genome["bam"])
    expected = [
        h.replace("SO:coordinate", "SO:unsorted") for h in _header(genome["bam"])
    ]
    assert _header(out) == expected
    assert "SO:coordinate" in _header(genome["bam"])[0]


def test_unsort_cram_keeps_it_readable_with_the_reference(
    genome, tmp_path, no_remote_refs
):
    cram = _cram(genome, tmp_path)
    _bam(
        "unsort",
        "--input",
        str(cram),
        "--ref",
        genome["ref"],
        "--outdir",
        str(tmp_path / "o"),
    )
    out = tmp_path / "o" / "s_unsorted.cram"
    assert "SO:unsorted" in _header(out)[0]
    assert _records(out, genome["ref"]) == _records(genome["bam"])


# --- to-cram / to-bam -----------------------------------------------------


def test_to_cram_encodes_against_the_given_reference(genome, tmp_path, no_remote_refs):
    _bam(
        "to-cram",
        "--input",
        genome["bam"],
        "--ref",
        genome["ref"],
        "--outdir",
        str(tmp_path),
    )
    out = tmp_path / "s.cram"
    assert Path(f"{out}.crai").is_file()
    assert get_file_version(out).startswith("CRAM version 3.0")
    md5s = {
        line.split("\t")[1][3:]: next(t[3:] for t in line.split("\t") if t[:3] == "M5:")
        for line in _header(out)
        if line.startswith("@SQ")
    }
    assert md5s == {
        c: hashlib.md5(s.encode()).hexdigest() for c, s in genome["seqs"].items()
    }
    assert _records(out, genome["ref"]) == _records(genome["bam"])


def test_to_cram_version_and_region(genome, tmp_path, no_remote_refs):
    _bam(
        "to-cram", "--input", genome["bam"], "--ref", genome["ref"],
        "--outdir", str(tmp_path), "--cram-version", "3.1", "-r", "chr1",
    )  # fmt: skip
    out = tmp_path / "s.cram"
    assert get_file_version(out).startswith("CRAM version 3.1")
    assert {r[2] for r in _records(out, genome["ref"])} == {"chr1"}


def test_failed_to_cram_leaves_no_partial_output(genome, tmp_path, no_remote_refs):
    bam = _copy(genome, tmp_path)  # no index, so a region can't be read
    with pytest.raises(WGSExtractError, match="Conversion to CRAM failed"):
        _bam(
            "to-cram", "--input", str(bam), "--ref", genome["ref"],
            "--outdir", str(tmp_path / "o"), "-r", "chr1",
        )  # fmt: skip
    assert list((tmp_path / "o").iterdir()) == []


def test_region_naming_no_contig_in_the_file_is_not_silently_empty(
    genome, tmp_path, no_remote_refs
):
    cram = _cram(genome, tmp_path)
    out_dir = tmp_path / "o"
    try:
        _bam(
            "to-bam", "--input", str(cram), "--ref", genome["ref"],
            "--outdir", str(out_dir), "-r", "2",
        )  # fmt: skip
    except WGSExtractError:
        return  # refusing is fine
    assert {r[2] for r in _records(out_dir / "s.bam")} == {"chr2"}


def test_to_cram_without_a_reference_embeds_one(genome, tmp_path, no_remote_refs):
    # htslib falls back to embedding a consensus of the reads (embed_ref=2),
    # so the CRAM decodes on its own.
    _bam("to-cram", "--input", genome["bam"], "--outdir", str(tmp_path))
    assert _records(tmp_path / "s.cram") == _records(genome["bam"])


def test_to_cram_never_encodes_against_a_mismatched_library_genome(
    genome, tmp_path, no_remote_refs, monkeypatch
):
    # The library's only genome has a 6 kb chr1; the BAM's chr1 is 5 kb.
    lib = tmp_path / "lib" / "genomes"
    lib.mkdir(parents=True)
    rng = random.Random(3)
    plain = lib / "hs38.fa"
    plain.write_text(
        "".join(
            f">{c}\n{''.join(rng.choice('ACGT') for _ in range(n + 1000))}\n"
            for c, n in LENGTHS.items()
        )
    )
    run_command(["bgzip", str(plain)])
    run_command(["samtools", "faidx", f"{plain}.gz"])
    monkeypatch.setitem(settings, "reference_library", str(tmp_path / "lib"))

    try:
        _bam("to-cram", "--input", genome["bam"], "--outdir", str(tmp_path / "o"))
    except WGSExtractError:
        return  # refusing is fine
    assert _records(tmp_path / "o" / "s.cram", genome["ref"]) == _records(genome["bam"])


def test_to_cram_of_a_cram_refuses_to_overwrite_it(genome, tmp_path, no_remote_refs):
    cram = _cram(genome, tmp_path)
    before = cram.read_bytes()
    with pytest.raises(WGSExtractError, match="overwrite the input"):
        _bam("to-cram", "--input", str(cram), "--ref", genome["ref"])
    assert cram.read_bytes() == before


def test_to_bam_of_a_bam_refuses_to_overwrite_it(genome, tmp_path):
    bam = _copy(genome, tmp_path, ".bai")
    before = bam.read_bytes()
    with pytest.raises(WGSExtractError, match="overwrite the input"):
        _bam("to-bam", "--input", str(bam))
    assert bam.read_bytes() == before


def test_to_bam_decodes_a_cram_with_its_reference(genome, tmp_path, no_remote_refs):
    cram = _cram(genome, tmp_path)
    out_dir = tmp_path / "out"
    _bam(
        "to-bam", "--input", str(cram), "--ref", genome["ref"], "--outdir", str(out_dir)
    )
    out = out_dir / "s.bam"
    assert Path(f"{out}.bai").is_file()
    assert _records(out) == _records(genome["bam"])
    assert _header(out)[-1] == "@RG\tID:L1\tSM:S1\tPL:ILLUMINA"


def test_to_bam_region(genome, tmp_path, no_remote_refs):
    cram = _cram(genome, tmp_path)
    _bam(
        "to-bam",
        "--input",
        str(cram),
        "--ref",
        genome["ref"],
        "--outdir",
        str(tmp_path / "o"),
        "-r",
        "chr2:400-800",
    )
    assert {r[0] for r in _records(tmp_path / "o" / "s.bam")} == {"p2"}


def test_to_bam_of_cram_without_its_reference_leaves_no_partial_bam(
    genome, tmp_path, no_remote_refs
):
    cram = _cram(genome, tmp_path)
    # The CRAM's @SQ UR: names the FASTA; move it so htslib can't find it.
    hidden = tmp_path / "elsewhere"
    hidden.mkdir()
    ref = Path(genome["ref"])
    for p in ref.parent.glob("ref.fa*"):
        shutil.copy(p, hidden / p.name)
    header = _samtools("view", "-H", str(cram)).replace(str(ref), "/nonexistent/ref.fa")
    (tmp_path / "h.sam").write_text(header)
    moved = tmp_path / "m.cram"
    with open(moved, "wb") as f:
        run_command(
            ["samtools", "reheader", str(tmp_path / "h.sam"), str(cram)], stdout=f
        )

    out_dir = tmp_path / "out"
    with pytest.raises(WGSExtractError, match="Conversion to BAM failed"):
        _bam("to-bam", "--input", str(moved), "--outdir", str(out_dir))
    assert list(out_dir.iterdir()) == []
    # With the reference named, the same file converts.
    _bam(
        "to-bam",
        "--input",
        str(moved),
        "--ref",
        str(hidden / "ref.fa"),
        "--outdir",
        str(out_dir),
    )
    assert _records(out_dir / "m.bam") == _records(genome["bam"])


# --- unalign --------------------------------------------------------------


def _fastq(path: Path) -> list[tuple[str, str]]:
    lines = path.read_text().splitlines()
    return [(lines[i][1:], lines[i + 1]) for i in range(0, len(lines), 4)]


def test_unalign_writes_mates_in_step_in_sequenced_orientation(genome, tmp_path):
    _bam(
        "unalign", "--input", genome["bam"], "--outdir", str(tmp_path),
        "--r1", "r1.fq", "--r2", "r2.fq", "--se", "se.fq",
    )  # fmt: skip
    r1, r2 = _fastq(tmp_path / "r1.fq"), _fastq(tmp_path / "r2.fq")
    assert [n for n, _ in r1] == [n for n, _ in r2]  # pairs stay together
    assert sorted(n for n, _ in r1) == ["p0", "p1", "p2", "p3", "u0"]
    s = genome["seqs"]
    reads1, reads2 = dict(r1), dict(r2)
    # Secondary/supplementary copies are dropped; reverse-strand mates are
    # restored to the strand they were sequenced from.
    assert reads1["p0"] == s["chr1"][99:149]
    assert reads2["p0"] == _revcomp(s["chr1"][299:349])
    assert reads2["p3"] == "A" * 50 and reads2["u0"] == "G" * 50
    assert _fastq(tmp_path / "se.fq") == [("s0", s["chr1"][2999:3049])]
    assert not list(tmp_path.glob(".collate_*"))


def test_unalign_region_drops_mates_outside_it(genome, tmp_path):
    _bam(
        "unalign", "--input", genome["bam"], "--outdir", str(tmp_path),
        "--r1", "r1.fq", "--r2", "r2.fq", "-r", "chr1:1-1500",
    )  # fmt: skip
    assert sorted(n for n, _ in _fastq(tmp_path / "r1.fq")) == ["p0", "p1"]
    assert sorted(n for n, _ in _fastq(tmp_path / "r2.fq")) == ["p0", "p1"]


def test_unalign_warns_that_read_groups_are_merged(genome, tmp_path, caplog):
    bam = str(tmp_path / "two_rg.bam")
    shutil.copy(genome["bam"], bam)
    _reheader(bam, lambda h: [*h, "@RG\tID:L2\tSM:S1\tPL:ILLUMINA"])
    _bam(
        "unalign", "--input", bam, "--outdir", str(tmp_path),
        "--r1", "r1.fq", "--r2", "r2.fq",
    )  # fmt: skip
    assert "2 read groups (L1, L2)" in caplog.text
    assert "bam to-ubam" in caplog.text


def test_unalign_failure_removes_partial_fastqs(genome, tmp_path):
    with pytest.raises(WGSExtractError):
        _bam(
            "unalign", "--input", genome["bam"], "--outdir", str(tmp_path),
            "--r1", "r1.fq", "--r2", str(tmp_path / "missing" / "r2.fq"),
        )  # fmt: skip
    assert not (tmp_path / "r1.fq").exists()


# --- identify -------------------------------------------------------------


def _signature(lengths: dict[str, int]) -> str:
    body = "".join(f"SN:{c.upper()}\tLN:{n}\n" for c, n in sorted(lengths.items()))
    return hashlib.md5(body.encode()).hexdigest()


def test_identify_logs_the_sequence_dictionary_signature(
    genome, tmp_path, caplog, no_remote_refs
):
    cram = _cram(genome, tmp_path)
    with caplog.at_level(logging.INFO):
        _bam("identify", "--input", genome["bam"])
        # A CRAM's header is readable without its reference.
        _bam("identify", "--input", str(cram))
    assert caplog.text.count(_signature(LENGTHS)) == 2


def test_identify_signature_ignores_contig_name_case_and_order(tmp_path):
    a = sam_to_bam(
        ["@SQ\tSN:chr2\tLN:3000", "@SQ\tSN:chr1\tLN:5000"], tmp_path / "a.bam"
    )
    b = sam_to_bam(
        ["@SQ\tSN:CHR1\tLN:5000", "@SQ\tSN:CHR2\tLN:3000"], tmp_path / "b.bam"
    )
    assert calculate_bam_md5(a) == calculate_bam_md5(b) == _signature(LENGTHS)


# --- merge ----------------------------------------------------------------


def _reheader(path: str, edit) -> None:
    """Rewrite a BAM's header in place through ``edit(lines) -> lines``."""
    lines = edit(_samtools("view", "-H", path).splitlines())
    sam = Path(path).with_suffix(".h.sam")
    sam.write_text("\n".join(lines) + "\n")
    with open(f"{path}.tmp", "wb") as f:
        run_command(["samtools", "reheader", "-P", str(sam), path], stdout=f)
    Path(f"{path}.tmp").replace(path)


def _split(genome, tmp_path: Path, rg: bool = True) -> tuple[str, str]:
    """The fixture's chr1 reads in a.bam and its other reads in b.bam."""
    paths = []
    for name, region in (("a", ["chr1"]), ("b", ["chr2", "*"])):
        out = str(tmp_path / f"{name}.bam")
        strip = [] if rg else ["--remove-tag", "RG"]
        run_command(
            ["samtools", "view", "-b", *strip, "-o", out, genome["bam"], *region]
        )
        if not rg:
            _reheader(out, lambda h: [x for x in h if not x.startswith("@RG")])
        paths.append(out)
    return paths[0], paths[1]


def test_merge_to_cram_uses_the_reference(genome, tmp_path, no_remote_refs):
    a, b = _split(genome, tmp_path)
    out_dir = tmp_path / "out"
    _bam("merge", a, b, "--cram", "--ref", genome["ref"], "--outdir", str(out_dir))
    out = out_dir / "merged.cram"
    assert Path(f"{out}.crai").is_file()
    assert _records(out, genome["ref"]) == _records(genome["bam"])


def test_merge_to_cram_needs_a_reference(genome, tmp_path):
    a, b = _split(genome, tmp_path)
    with pytest.raises(WGSExtractError, match="--ref is required"):
        _bam("merge", a, b, "--cram", "--outdir", str(tmp_path / "out"))


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--output", "x.cram"], "does not match the output format"),
        (["--output", "a.bam"], "would overwrite an input"),
    ],
)
def test_merge_rejects_bad_outputs(genome, tmp_path, extra, message):
    a, b = _split(genome, tmp_path)
    before = Path(a).read_bytes()
    with pytest.raises(WGSExtractError, match=message):
        _bam("merge", a, b, "--outdir", str(tmp_path), *extra)
    assert Path(a).read_bytes() == before


def test_merge_rejects_repeated_unsorted_or_single_inputs(genome, tmp_path):
    a, b = _split(genome, tmp_path)
    with pytest.raises(WGSExtractError, match="more than once"):
        _bam("merge", a, a)
    with pytest.raises(WGSExtractError, match="at least two"):
        _bam("merge", a)
    named = tmp_path / "n.bam"
    run_command(["samtools", "sort", "-n", "-o", str(named), b])
    with pytest.raises(WGSExtractError, match="not coordinate-sorted"):
        _bam("merge", a, str(named))


def test_merge_output_name_gets_the_extension(genome, tmp_path):
    a, b = _split(genome, tmp_path)
    _bam("merge", a, b, "--output", "lanes", "--outdir", str(tmp_path / "o"))
    assert (tmp_path / "o" / "lanes.bam.bai").is_file()


def test_merge_keeps_conflicting_read_groups_apart(genome, tmp_path, caplog):
    a, b = _split(genome, tmp_path)
    _reheader(b, lambda h: [x.replace("SM:S1", "SM:S2") for x in h])
    with caplog.at_level(logging.WARNING):
        _bam("merge", a, b, "--outdir", str(tmp_path / "o"))
    assert "defined differently" in caplog.text
    out = tmp_path / "o" / "merged.bam"
    assert len([h for h in _header(out) if h.startswith("@RG")]) == 2


def test_merge_failure_leaves_no_partial_output(genome, tmp_path):
    a, b = _split(genome, tmp_path)
    Path(b).write_bytes(Path(b).read_bytes()[:-200])  # truncated BGZF
    with pytest.raises(WGSExtractError, match="Merging failed"):
        _bam("merge", a, b, "--outdir", str(tmp_path / "o"))
    assert list((tmp_path / "o").iterdir()) == []


def test_merge_attach_rg_declares_the_read_groups_it_tags(genome, tmp_path):
    a, b = _split(genome, tmp_path, rg=False)
    _bam("merge", a, b, "--attach-rg", "--outdir", str(tmp_path / "o"))
    out = tmp_path / "o" / "merged.bam"
    assert {r[6] for r in _records(out)} == {"RG:Z:a", "RG:Z:b"}
    declared = {h.split("\t")[1] for h in _header(out) if h.startswith("@RG")}
    assert declared == {"ID:a", "ID:b"}
