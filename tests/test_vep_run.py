"""`wgsextract vep [run]` against a fake `vep` on PATH that logs its command
line and writes what VEP would (a bgzipped copy of its input, or a text table)."""

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fake_tools import python_tool
from tests.htslib_helpers import (
    bgzip_index,
    needs_htslib,
    run_wgsextract,
    sam_to_bam,
    vcf_calls,
    write_fasta,
)
from wgsextract_cli.core.utils import WGSExtractError

pytestmark = needs_htslib

_HEADER = (
    "##fileformat=VCFv4.2\n"
    "##contig=<ID=1,length=1000>\n"
    "##contig=<ID=2,length=1000>\n"
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
)

_FAKE_VEP = """import json, subprocess, sys
argv = sys.argv[1:]
if "-i" not in argv:  # version probes
    sys.exit(0)
with open({log!r}, "a") as f:
    f.write(json.dumps(argv) + "\\n")
inp, out = argv[argv.index("-i") + 1], argv[argv.index("-o") + 1]
if "--compress_output" in argv:
    subprocess.run([{bcftools!r}, "view", "-Oz", "-o", out, inp], check=True)
else:
    open(out, "w").write("#Uploaded_variation\\tLocation\\n")
open(out + "_summary.html", "w").write("<html/>")
sys.exit({exit_code})
"""


@pytest.fixture
def fake_vep(tmp_path, monkeypatch):
    """Install a fake `vep`; returns a function giving the logged command lines.
    ``fake_vep.install(exit_code=..., dirname=...)`` reinstalls it."""
    log = tmp_path / "vep_calls.jsonl"

    def install(exit_code: int = 0, dirname: str = "fakebin") -> None:
        bindir = tmp_path / dirname
        bindir.mkdir(exist_ok=True)
        source = _FAKE_VEP.format(
            log=str(log), bcftools=shutil.which("bcftools"), exit_code=exit_code
        )
        python_tool(bindir, "vep", source)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        from wgsextract_cli.core.dependencies import clear_tool_caches

        clear_tool_caches()

    def calls() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    install()
    calls.install = install  # type: ignore[attr-defined]
    return calls


def _vcf(path: Path, records: str = "1\t10\t.\tA\tG\t50\tPASS\t.\tGT\t0/1\n") -> str:
    plain = path.with_suffix("")
    plain.write_text(_HEADER + records)
    return bgzip_index(plain, path)


def _vep(*argv: str) -> None:
    run_wgsextract("vep", *argv)


def _value(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


def test_vcf_run_writes_indexed_output_and_moves_report(tmp_path, fake_vep):
    vcf = _vcf(tmp_path / "s.vcf.gz")
    cache = tmp_path / "cache"
    cache.mkdir()

    _vep("-i", vcf, "--vep-cache", str(cache), "--vep-assembly", "GRCh38", "-t", "3")

    out = tmp_path / "s_vep.vcf.gz"
    assert vcf_calls(out) == [("1", "10", "A", "G", "0/1")]
    assert Path(f"{out}.tbi").exists()
    assert (tmp_path / "s_vep.vcf_summary.html").exists()
    assert not list(tmp_path.glob(".wgse_vep_*"))
    [cmd] = fake_vep()
    assert _value(cmd, "-i") == vcf
    assert Path(_value(cmd, "-o")).parent.name.startswith(".wgse_vep_")
    assert _value(cmd, "--fork") == "3"
    assert _value(cmd, "--assembly") == "GRCh38"
    assert _value(cmd, "--dir_cache") == str(cache)
    assert _value(cmd, "--cache_version") == "116"
    assert _value(cmd, "--compress_output") == "bgzip"
    assert {"--vcf", "--offline", "--everything"} <= set(cmd)
    assert "--database" not in cmd and "--overlaps" not in cmd


def test_existing_output_is_skipped_unless_forced(tmp_path, fake_vep):
    vcf = _vcf(tmp_path / "s.vcf.gz")
    (tmp_path / "s_vep.vcf.gz").write_text("old")

    _vep("-i", vcf, "--vep-cache", str(tmp_path))
    assert fake_vep() == []
    assert (tmp_path / "s_vep.vcf.gz").read_text() == "old"

    _vep("-i", vcf, "--vep-cache", str(tmp_path), "--force")
    assert len(fake_vep()) == 1
    assert vcf_calls(tmp_path / "s_vep.vcf.gz") == [("1", "10", "A", "G", "0/1")]


def test_missing_cache_falls_back_to_database(tmp_path, fake_vep):
    vcf = _vcf(tmp_path / "s.vcf.gz")
    missing = tmp_path / "no_cache"

    _vep("-i", vcf, "--vep-cache", str(missing), "--vep-cache-version", "115")

    [cmd] = fake_vep()
    assert "--database" in cmd and "--offline" not in cmd
    assert _value(cmd, "--dir_cache") == str(missing)
    assert _value(cmd, "--cache_version") == "115"


def test_unindexed_vcf_is_indexed_before_vep(tmp_path, fake_vep):
    vcf = _vcf(tmp_path / "s.vcf.gz")
    Path(f"{vcf}.tbi").unlink()

    _vep("-i", vcf, "--vep-cache", str(tmp_path))

    assert Path(f"{vcf}.tbi").exists()
    assert len(fake_vep()) == 1


@pytest.mark.parametrize(
    ("fmt", "name"), [("tab", "s_vep.txt"), ("json", "s_vep.json")]
)
def test_tab_and_json_are_moved_into_outdir_once_complete(
    tmp_path, fake_vep, fmt, name
):
    vcf = _vcf(tmp_path / "s.vcf.gz")
    outdir = tmp_path / "new" / "out"

    _vep("-i", vcf, "-o", str(outdir), "--format", fmt, "--vep-cache", str(tmp_path))

    [cmd] = fake_vep()
    assert f"--{fmt}" in cmd and "--vcf" not in cmd
    assert "--compress_output" not in cmd
    assert Path(_value(cmd, "-o")).parent.name.startswith(".wgse_vep_")
    assert (outdir / name).read_text().startswith("#Uploaded_variation")
    assert sorted(p.name for p in outdir.iterdir()) == [name, f"{name}_summary.html"]


@pytest.mark.parametrize(
    ("name", "extra", "sv"),
    [
        ("sample_sv.vcf.gz", [], True),
        ("cnv.vcf.gz", [], True),
        ("sample.vcf.gz", ["--vcf-type", "sv"], True),
        ("sample_sv.vcf.gz", ["--vcf-type", "snp-indel"], False),
    ],
)
def test_structural_variants_get_overlap_options(tmp_path, fake_vep, name, extra, sv):
    vcf = _vcf(tmp_path / name)

    _vep("-i", vcf, "--vep-cache", str(tmp_path), *extra)

    [cmd] = fake_vep()
    assert ("--overlaps" in cmd) is sv
    if sv:
        assert _value(cmd, "--max_sv_size") == "300000000"


def test_preset_and_raw_vep_args_are_appended(tmp_path, fake_vep):
    vcf = _vcf(tmp_path / "s.vcf.gz")

    _vep(
        "-i", vcf, "--vep-cache", str(tmp_path), "--preset", "fast",
        "--vep-args", "--pick --plugin 'Foo,a b'",
    )  # fmt: skip

    [cmd] = fake_vep()
    assert "--everything" not in cmd
    assert cmd[-5:-2] == ["--pick", "--plugin", "Foo,a b"]
    assert cmd.index("--symbol") < cmd.index("--pick")


def test_add_chr_feeds_vep_a_prefixed_copy(tmp_path, fake_vep):
    vcf = _vcf(
        tmp_path / "s.vcf.gz",
        "1\t10\t.\tA\tG\t50\tPASS\t.\tGT\t0/1\n2\t20\t.\tC\tT\t50\tPASS\t.\tGT\t1/1\n",
    )

    _vep("-i", vcf, "--vep-cache", str(tmp_path), "--add-chr")

    [cmd] = fake_vep()
    assert Path(_value(cmd, "-i")).name == "with_chr.vcf"
    assert vcf_calls(tmp_path / "s_vep.vcf.gz") == [
        ("chr1", "10", "A", "G", "0/1"),
        ("chr2", "20", "C", "T", "1/1"),
    ]
    assert vcf_calls(vcf)[0][0] == "1"  # the input is left alone


def test_vep_failure_fails_the_run_and_leaves_no_output(tmp_path, fake_vep):
    fake_vep.install(exit_code=2)
    vcf = _vcf(tmp_path / "s.vcf.gz")

    with pytest.raises(WGSExtractError, match="VEP tasks failed"):
        _vep("-i", vcf, "--vep-cache", str(tmp_path))

    assert len(fake_vep()) == 1
    assert not list(tmp_path.glob("s_vep*"))
    assert not list(tmp_path.glob(".wgse_vep_*"))


def test_failed_tab_run_is_retried_next_time(tmp_path, fake_vep):
    fake_vep.install(exit_code=2)
    vcf = _vcf(tmp_path / "s.vcf.gz")
    with pytest.raises(WGSExtractError):
        _vep("-i", vcf, "--format", "tab", "--vep-cache", str(tmp_path))

    fake_vep.install(exit_code=0)
    _vep("-i", vcf, "--format", "tab", "--vep-cache", str(tmp_path))
    assert len(fake_vep()) == 2


def test_directory_batch_annotates_each_file_and_summarises(tmp_path, fake_vep, capsys):
    batch = tmp_path / "batch"
    batch.mkdir()
    _vcf(batch / "a.vcf.gz")
    _vcf(batch / "b.vcf.gz")
    (batch / "notes.txt").write_text("not a VCF")
    out = tmp_path / "out"
    out.mkdir()
    (out / "b_vep.vcf.gz").write_text("done")

    _vep("-i", str(batch), "-o", str(out), "--vep-cache", str(tmp_path))

    assert [Path(_value(c, "-i")).name for c in fake_vep()] == ["a.vcf.gz"]
    assert vcf_calls(out / "a_vep.vcf.gz")
    summary = capsys.readouterr().out
    assert "VEP Batch Summary" in summary
    assert "a.vcf.gz" in summary and "SUCCESS" in summary
    assert "b.vcf.gz" in summary and "SKIPPED" in summary


def test_directory_rerun_does_not_annotate_its_own_outputs(tmp_path, fake_vep):
    batch = tmp_path / "batch"
    batch.mkdir()
    _vcf(batch / "a.vcf.gz")
    _vep("-i", str(batch), "--vep-cache", str(tmp_path))

    _vep("-i", str(batch), "--vep-cache", str(tmp_path))

    assert not list(batch.glob("*_vep_vep*"))
    assert len(fake_vep()) == 1


def test_batch_inputs_sharing_a_stem_get_separate_outputs(tmp_path, fake_vep):
    batch = tmp_path / "batch"
    batch.mkdir()
    _vcf(batch / "s.chr1.vcf.gz")
    _vcf(batch / "s.chr2.vcf.gz", "2\t20\t.\tC\tT\t50\tPASS\t.\tGT\t1/1\n")

    _vep("-i", str(batch), "--vep-cache", str(tmp_path))

    assert len(fake_vep()) == 2
    assert len(list(batch.glob("*_vep*.vcf.gz"))) == 2


def test_unsupported_or_empty_inputs_are_rejected(tmp_path, fake_vep):
    (tmp_path / "x.txt").write_text("")
    with pytest.raises(WGSExtractError, match="Unsupported VEP input"):
        _vep("-i", str(tmp_path / "x.txt"))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(WGSExtractError, match="No valid genomic files"):
        _vep("-i", str(empty))
    assert fake_vep() == []


def test_vep_under_a_path_with_spaces_runs(tmp_path, fake_vep):
    fake_vep.install(dirname="my tools")
    vcf = _vcf(tmp_path / "s.vcf.gz")

    _vep("-i", vcf, "--vep-cache", str(tmp_path))

    assert len(fake_vep()) == 1


def test_bcf_input_reaches_vep_as_vcf(tmp_path, fake_vep):
    from wgsextract_cli.core.utils import run_command

    vcf = _vcf(tmp_path / "s.vcf.gz")
    bcf = tmp_path / "s.bcf"
    run_command(["bcftools", "view", "-Ob", "-o", str(bcf), vcf])
    run_command(["bcftools", "index", str(bcf)])

    _vep("-i", str(bcf), "--vep-cache", str(tmp_path))

    [cmd] = fake_vep()
    assert not _value(cmd, "-i").endswith(".bcf")


def test_region_limits_a_vcf_input(tmp_path, fake_vep):
    vcf = _vcf(
        tmp_path / "s.vcf.gz",
        "1\t10\t.\tA\tG\t50\tPASS\t.\tGT\t0/1\n2\t20\t.\tC\tT\t50\tPASS\t.\tGT\t1/1\n",
    )

    _vep("-i", vcf, "--vep-cache", str(tmp_path), "-r", "2")

    assert [c[0] for c in vcf_calls(tmp_path / "s_vep.vcf.gz")] == ["2"]


def test_cache_downloaded_into_reference_library_is_used(tmp_path, fake_vep):
    lib = tmp_path / "lib"
    (lib / "vep" / "homo_sapiens" / "116_GRCh38").mkdir(parents=True)
    vcf = _vcf(tmp_path / "s.vcf.gz")

    _vep("-i", vcf, "--ref", str(lib))

    [cmd] = fake_vep()
    assert _value(cmd, "--dir_cache") == str(lib / "vep")
    assert "--offline" in cmd


# --- BAM/CRAM input: bcftools calls variants first ------------------------------

_GENOME = "ACGTTGCAAGCTTCGATCGGATCCTAGCATGCAAGTCCGATAGCTAGCTTACGGATCAGTCAGGATC" * 8


def _bam_with_snps(tmp_path: Path) -> tuple[str, str]:
    """chr1 reference and a BAM of 20 reads homozygous for a SNP at 101 and 301."""
    fasta = write_fasta(tmp_path / "ref.fa", {"chr1": _GENOME})
    alt = {"A": "C", "C": "G", "G": "T", "T": "A"}
    lines = ["@HD\tVN:1.6\tSO:unsorted", f"@SQ\tSN:chr1\tLN:{len(_GENOME)}"]
    for snp in (101, 301):
        start = snp - 50  # 1-based start of a 100 bp read centred on the SNP
        seq = list(_GENOME[start - 1 : start + 99])
        seq[snp - start] = alt[seq[snp - start]]
        for i in range(20):
            lines.append(
                f"r{snp}_{i}\t0\tchr1\t{start}\t60\t100M\t*\t0\t0\t"
                f"{''.join(seq)}\t{'I' * 100}"
            )
    bam = sam_to_bam(lines, tmp_path / "s.bam")
    return str(fasta), bam


def test_bam_input_calls_variants_in_region_then_annotates(tmp_path, fake_vep):
    fasta, bam = _bam_with_snps(tmp_path)

    _vep(
        "-i", bam, "--ref", fasta, "-r", "chr1:250-350",
        "--ploidy", "1", "--vep-cache", str(tmp_path),
    )  # fmt: skip

    [cmd] = fake_vep()
    assert Path(_value(cmd, "-i")).name == "variants.vcf.gz"
    assert _value(cmd, "--fasta") == fasta
    calls = vcf_calls(tmp_path / "s_vep.vcf.gz")
    assert [(c[0], c[1], c[4]) for c in calls] == [("chr1", "301", "1")]


def test_bam_without_reference_fails_without_running_vep(tmp_path, fake_vep):
    _, bam = _bam_with_snps(tmp_path)
    Path(tmp_path, "ref.fa").unlink()

    with pytest.raises(WGSExtractError, match="VEP tasks failed"):
        _vep("-i", bam, "--vep-cache", str(tmp_path))
    assert fake_vep() == []


# --- assembly and FASTA choice ---------------------------------------------------


def _command(lib, **overrides):
    from wgsextract_cli.commands._vep_run import _build_vep_command

    args = SimpleNamespace(
        format="vcf", vep_assembly=None, vep_cache="/no/such/cache",
        vep_cache_version="116", vep_args=None, preset="everything",
    )  # fmt: skip
    vars(args).update(overrides)
    return _build_vep_command(args, "in.vcf.gz", "out.vcf", "snp-indel", lib, "2")


@pytest.mark.parametrize(
    ("build", "assembly"), [("hg38", "GRCh38"), ("hg19", "GRCh37"), (None, None)]
)
def test_assembly_follows_the_library_build(build, assembly):
    cmd = _command(SimpleNamespace(build=build, fasta="/r/g.fa", vep_cache=None))
    assert _value(cmd, "--fasta") == "/r/g.fa"
    if assembly:
        assert _value(cmd, "--assembly") == assembly
    else:
        assert "--assembly" not in cmd


def test_library_cache_used_when_no_cache_option(tmp_path):
    lib = SimpleNamespace(build="hg38", fasta=None, vep_cache=str(tmp_path))
    cmd = _command(lib, vep_cache=None)
    assert _value(cmd, "--dir_cache") == str(tmp_path)
    assert "--offline" in cmd and "--fasta" not in cmd


@pytest.mark.parametrize(
    ("fasta", "forced", "kept"),
    [
        ("/lib/hg38.fa", "GRCh37", False),
        ("/lib/hs37d5.fa", "GRCh38", False),
        ("/lib/GRCh38.fa", "GRCh38", True),
        ("/lib/genome.fa", "GRCh37", True),
    ],
)
def test_forced_assembly_drops_a_conflicting_fasta(fasta, forced, kept):
    from wgsextract_cli.commands._vep_run import _apply_forced_assembly

    lib = SimpleNamespace(build="hg38", fasta=fasta)
    _apply_forced_assembly(lib, forced)  # type: ignore[arg-type]
    assert lib.build == ("hg38" if forced == "GRCh38" else "hg19")
    assert lib.fasta == (fasta if kept else None)
