"""`vcf freebayes` and `vcf gatk` (CPU HaplotypeCaller) on a tiny genome."""

import random
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from tests.htslib_helpers import (
    needs_tools,
    require_runnable,
    run_wgsextract,
    sam_to_bam,
    vcf_calls,
    write_fasta,
)
from wgsextract_cli.commands.vcf import callers
from wgsextract_cli.core import paths
from wgsextract_cli.core.utils import WGSExtractError, run_command

SNP = 1000  # 1-based position of the sample's homozygous SNP


@pytest.fixture(scope="module")
def genome(tmp_path_factory) -> Namespace:
    """A 3 kb chr1, and a sample homozygous for one SNP at SNP, as a BAM and a
    CRAM with 20x single-end coverage over 500-2500."""
    work = tmp_path_factory.mktemp("callers")
    rng = random.Random(7)
    ref = "".join(rng.choice("ACGT") for _ in range(3000))
    alt = {"A": "C", "C": "G", "G": "T", "T": "A"}[ref[SNP - 1]]
    sample = ref[: SNP - 1] + alt + ref[SNP:]
    fasta = write_fasta(work / "ref.fa", {"chr1": ref})
    lines = ["@HD\tVN:1.6\tSO:unsorted", "@SQ\tSN:chr1\tLN:3000", "@RG\tID:rg\tSM:S"]
    for i, start in enumerate(range(500, 2400, 5)):
        lines.append(
            f"r{i}\t{16 if i % 2 else 0}\tchr1\t{start}\t60\t100M\t*\t0\t0\t"
            f"{sample[start - 1 : start + 99]}\t{'I' * 100}\tRG:Z:rg"
        )
    bam = sam_to_bam(lines, work / "s.bam")
    cram = str(work / "s.cram")
    run_command(["samtools", "view", "-C", "-T", str(fasta), "-o", cram, bam])
    run_command(["samtools", "index", cram])
    return Namespace(
        ref=str(fasta),
        bam=bam,
        cram=cram,
        call=("chr1", str(SNP), ref[SNP - 1], alt, "1/1"),
    )


def _call(caller: str, genome: Namespace, input_path: str, outdir: Path, *extra: str):
    require_runnable(caller, "samtools", "tabix")
    run_wgsextract("vcf", caller, "--input", input_path, "--ref", genome.ref,
                   "--outdir", str(outdir), *extra)  # fmt: skip
    vcf = outdir / f"{caller}.vcf.gz"
    assert Path(f"{vcf}.tbi").is_file()
    return [call[:5] for call in vcf_calls(vcf)]


# --- freebayes -------------------------------------------------------------------

needs_freebayes = needs_tools("freebayes", "samtools", "bcftools", "tabix")


@needs_freebayes
def test_freebayes_calls_the_snp_from_a_bam(genome, tmp_path):
    assert _call("freebayes", genome, genome.bam, tmp_path) == [genome.call]


@needs_freebayes
def test_freebayes_streams_a_cram_through_samtools(genome, tmp_path):
    assert _call("freebayes", genome, genome.cram, tmp_path) == [genome.call]


@needs_freebayes
def test_freebayes_decompresses_a_gzipped_reference_and_removes_it(genome, tmp_path):
    require_runnable("freebayes", "samtools", "tabix")
    gz = tmp_path / "ref" / "ref.fa.gz"
    gz.parent.mkdir()
    with open(gz, "wb") as f:
        run_command(["bgzip", "-c", genome.ref], stdout=f)
    run_command(["samtools", "faidx", str(gz)])
    outdir = tmp_path / "out"
    run_wgsextract("vcf", "freebayes", "--input", genome.bam, "--ref", str(gz),
                   "--outdir", str(outdir))  # fmt: skip
    assert vcf_calls(outdir / "freebayes.vcf.gz") == [genome.call]
    assert not list(outdir.glob(".wgse_ref_*"))
    assert not (gz.parent / "ref.fa").exists()


@needs_freebayes
def test_freebayes_with_nothing_in_the_region_warns(genome, tmp_path, caplog):
    assert (
        _call("freebayes", genome, genome.bam, tmp_path, "-r", "chr1:2000-2100") == []
    )
    assert "No variants were called in chr1:2000-2100" in caplog.text


@needs_freebayes
def test_freebayes_bam_region_includes_its_first_base(genome, tmp_path):
    region = f"chr1:{SNP}-{SNP + 50}"
    assert _call("freebayes", genome, genome.bam, tmp_path, "-r", region) == [
        genome.call
    ]


@needs_freebayes
def test_freebayes_cram_region_excludes_calls_outside_it(genome, tmp_path):
    region = f"chr1:{SNP + 10}-{SNP + 50}"
    assert _call("freebayes", genome, genome.cram, tmp_path, "-r", region) == []


# --- GATK HaplotypeCaller --------------------------------------------------------


@needs_tools("gatk", "samtools", "tabix")
def test_gatk_calls_the_snp_from_a_regional_cram(genome, tmp_path):
    """One real GATK run (slow to start): the CRAM is cut to a temporary BAM,
    the missing .dict is made beside the reference, and nothing else is left."""
    require_runnable("gatk", "samtools", "tabix")
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir()
    ref = ref_dir / "ref.fa"
    ref.write_text(Path(genome.ref).read_text())
    run_command(["samtools", "faidx", str(ref)])
    outdir = tmp_path / "out"
    run_wgsextract(
        "vcf", "gatk", "--input", Path(genome.cram).as_posix(),
        "--ref", ref.as_posix(), "--outdir", outdir.as_posix(),
        "-r", "chr1:900-1100",
    )  # fmt: skip

    assert [c[:5] for c in vcf_calls(outdir / "gatk.vcf.gz")] == [genome.call]
    assert (ref_dir / "ref.dict").read_text().startswith("@HD")
    assert sorted(p.name for p in outdir.iterdir()) == [
        "gatk.vcf.gz",
        "gatk.vcf.gz.tbi",
    ]


def _mock_gatk(monkeypatch, tmp_path, run):
    """cmd_gatk with get_base_args and dependency checks stubbed; ``run``
    stands in for run_command."""
    lib = Namespace(dict_file=str(tmp_path / "ref.dict"))
    monkeypatch.setattr(callers, "verify_dependencies", lambda _tools: None)
    monkeypatch.setattr(
        callers, "get_base_args", lambda _a: ("2", str(tmp_path), "ref.fa", lib)
    )
    monkeypatch.setattr(callers, "get_tool_path", lambda tool: tool)
    monkeypatch.setattr(callers, "run_command", run)
    gatk_tmp = tmp_path / "gatk-tmp"
    gatk_tmp.mkdir()
    monkeypatch.setattr(callers, "_gatk_tmp_dir", lambda: str(gatk_tmp))
    return gatk_tmp


def test_gatk_failure_on_cram_cleans_up_and_hints_at_cram_versions(
    monkeypatch, tmp_path, caplog
):
    commands: list[list[str]] = []

    def run(cmd, **_kwargs):
        commands.append(cmd)
        if cmd[0] == "samtools" and cmd[1] == "view":
            Path(cmd[cmd.index("-o") + 1]).write_text("bam")
        elif cmd[0] == "samtools" and cmd[1] == "index":
            Path(cmd[2] + ".bai").write_text("bai")
        elif cmd[0] == "gatk":
            raise subprocess.CalledProcessError(2, cmd)

    gatk_tmp = _mock_gatk(monkeypatch, tmp_path, run)
    args = Namespace(input=str(tmp_path / "s.cram"), region="chr1:1-500", gpu=False)
    with pytest.raises(WGSExtractError, match="GATK failed"):
        callers.cmd_gatk(args)

    gatk = commands[-1]
    assert gatk[:2] == ["gatk", "HaplotypeCaller"]
    assert gatk[gatk.index("--tmp-dir") + 1] == str(gatk_tmp)
    assert gatk[gatk.index("-L") + 1] == str(tmp_path / "gatk_region.intervals")
    assert gatk[gatk.index("-I") + 1].endswith(".bam")
    assert "CRAM version 3.1" in caplog.text
    # The temporary BAM, its index, the intervals file and GATK's temp dir are gone.
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_gatk_dict_generation_failure_is_reported(monkeypatch, tmp_path):
    def run(cmd, **_kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    _mock_gatk(monkeypatch, tmp_path, run)
    lib = Namespace(dict_file=None)
    monkeypatch.setattr(
        callers, "get_base_args", lambda _a: ("2", str(tmp_path), "ref.fa", lib)
    )
    args = Namespace(input="s.bam", region=None, gpu=False)
    with pytest.raises(WGSExtractError, match="Failed to generate GATK dict"):
        callers.cmd_gatk(args)


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_gatk_native_numeric_warning_only_on_windows(
    monkeypatch, tmp_path, caplog, platform
):
    commands = []

    def run(cmd, **_kwargs):
        commands.append(cmd)
        raise subprocess.CalledProcessError(1, cmd)

    _mock_gatk(monkeypatch, tmp_path, run)
    monkeypatch.setattr(
        callers.ultima, "alignment_flow_data", lambda *_: Namespace(flow_aware=False)
    )
    monkeypatch.setattr(callers.sys, "platform", platform)
    args = Namespace(input="s.bam", region=None, gpu=False)
    with pytest.raises(WGSExtractError, match="GATK failed"):
        callers.cmd_gatk(args)
    assert commands[0][:2] == ["gatk", "HaplotypeCaller"]
    assert ("compression remains Java-based" in caplog.text) == (platform == "win32")


def test_failed_cram_conversion_removes_the_partial_bam(monkeypatch, tmp_path):
    def run(cmd, **_kwargs):
        Path(cmd[cmd.index("-o") + 1]).write_text("partial")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(callers, "get_tool_path", lambda tool: tool)
    monkeypatch.setattr(callers, "run_command", run)
    args = Namespace(input="s.cram", region="chr1:1-500")
    with pytest.raises(subprocess.CalledProcessError):
        callers._prepare_gatk_input(args, "ref.fa", str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_gatk_takes_each_comma_separated_region_as_an_interval(monkeypatch, tmp_path):
    intervals: list[str] = []

    def run(cmd, **_kwargs):
        if cmd[0] == "gatk":
            intervals.extend(Path(cmd[cmd.index("-L") + 1]).read_text().splitlines())

    _mock_gatk(monkeypatch, tmp_path, run)
    monkeypatch.setattr(callers, "ensure_vcf_indexed", lambda _v: None)
    monkeypatch.setattr(callers, "warn_if_no_variants", lambda *_a: None)
    args = Namespace(input="s.bam", region="chr20,chr21:1-500", gpu=False)
    callers.cmd_gatk(args)
    assert intervals == ["chr20", "chr21:1-500"]


def test_cleanup_tolerates_missing_temp_files(tmp_path):
    bam = tmp_path / "t.bam"
    bam.write_text("x")
    (tmp_path / "t.bam.csi").write_text("x")
    callers._cleanup_gatk_temp_input(str(bam))
    callers._cleanup_gatk_temp_input(None)
    assert list(tmp_path.iterdir()) == []


def test_gatk_scatter_with_one_worker_uses_the_unscattered_path(monkeypatch, tmp_path):
    commands = []
    _mock_gatk(monkeypatch, tmp_path, lambda cmd, **_: commands.append(cmd))
    monkeypatch.setattr(callers, "scatter_intervals", lambda *_: [[("chr1", 1, 100)]])
    monkeypatch.setattr(
        callers.ultima, "alignment_flow_data", lambda *_: Namespace(flow_aware=True)
    )
    monkeypatch.setattr(callers, "ensure_vcf_indexed", lambda _: None)
    monkeypatch.setattr(callers, "warn_if_no_variants", lambda *_: None)
    callers.cmd_gatk(
        Namespace(input="s.bam", region="chr1:1-100", gpu=False, scatter=True)
    )
    assert len(commands) == 1
    assert commands[0][:2] == ["gatk", "HaplotypeCaller"]
    assert "--flow-mode" in commands[0]
    assert "--java-options" not in commands[0]
    assert list(tmp_path.iterdir()) == []


def test_gatk_scatter_requires_flow_aware_input(monkeypatch, tmp_path):
    _mock_gatk(monkeypatch, tmp_path, lambda *_a, **_kw: pytest.fail("GATK started"))
    monkeypatch.setattr(callers, "scatter_intervals", lambda *_: [[("chr1", 1, 100)]])
    monkeypatch.setattr(
        callers.ultima, "alignment_flow_data", lambda *_: Namespace(flow_aware=False)
    )
    with pytest.raises(WGSExtractError, match="requires flow-aware Ultima"):
        callers.cmd_gatk(Namespace(input="s.bam", region=None, gpu=False, scatter=True))


def test_gatk_regional_cram_conversion_reads_each_requested_interval(
    monkeypatch, tmp_path
):
    commands = []
    monkeypatch.setattr(callers, "get_tool_path", lambda name: name)
    monkeypatch.setattr(
        callers, "run_command", lambda command, **_: commands.append(command)
    )
    _bam, temporary = callers._prepare_gatk_input(
        Namespace(input="s.cram", region="chr2:1-20, chr1:30-50"),
        "ref.fa",
        str(tmp_path),
    )
    assert commands[0][-3:] == ["s.cram", "chr2:1-20", "chr1:30-50"]
    assert "-M" in commands[0]
    callers._cleanup_gatk_temp_input(temporary)


def test_gatk_windows_argv_uses_native_local_paths(monkeypatch, tmp_path):
    import ntpath

    commands = []
    gatk_tmp = _mock_gatk(
        monkeypatch, tmp_path, lambda cmd, **_kwargs: commands.append(cmd)
    )
    lib = Namespace(dict_file=None)
    reference = "R:/Reference genome/ref.fa.gz"
    monkeypatch.setattr(paths, "sys", Namespace(platform="win32"))
    monkeypatch.setattr(
        callers, "get_base_args", lambda _: ("1", str(tmp_path), reference, lib)
    )
    monkeypatch.setattr(
        callers.ultima, "alignment_flow_data", lambda *_: Namespace(flow_aware=False)
    )
    monkeypatch.setattr(callers, "ensure_vcf_indexed", lambda _: None)
    monkeypatch.setattr(callers, "warn_if_no_variants", lambda *_: None)
    callers.cmd_gatk(
        Namespace(input="R:/Input data/sample.bam", region="chr1:1-500", gpu=False)
    )

    dictionary, gatk = commands
    assert dictionary == [
        "samtools",
        "dict",
        "-o",
        r"R:\Reference genome\ref.dict",
        r"R:\Reference genome\ref.fa.gz",
    ]
    assert lib.dict_file == r"R:\Reference genome\ref.dict"
    expected = {
        "-R": r"R:\Reference genome\ref.fa.gz",
        "-I": r"R:\Input data\sample.bam",
        "-O": ntpath.normpath(str(tmp_path / "gatk.vcf.gz")),
        "--tmp-dir": ntpath.normpath(str(gatk_tmp)),
        "-L": ntpath.normpath(str(tmp_path / "gatk_region.intervals")),
    }
    for flag, value in expected.items():
        assert gatk[gatk.index(flag) + 1] == value
    assert sorted(tmp_path.iterdir()) == []
