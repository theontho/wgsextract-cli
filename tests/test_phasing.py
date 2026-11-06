"""`vcf phase`: read-backed phasing on real HG002 ONT reads (#341).

tests/fixtures/ont_hg002_phasing holds ONT R10.4.1 reads trimmed to a 12 kb
window of chr20 (14,255,001-14,267,000) and GIAB v4.2.1's phased genotypes
there: 25 heterozygous variants, whose haplotypes switch from 1|0 to 0|1
between 14,258,707 and 14,259,373 (provenance.json; rebuilt by
scripts/ont_hg002_data.py phasing-fixture). minimap2, samtools, bcftools,
WhatsHap and LongPhase run for real.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_htslib, needs_tools, run_wgsextract
from wgsextract_cli import api
from wgsextract_cli.cli.parser import build_parser
from wgsextract_cli.cli.platform_support import verify_platform_support
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependencies import clear_tool_caches, get_tool_path
from wgsextract_cli.core.dependency_checks import MissingDependencyError
from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.utils import UsageError, WGSExtractError

FIXTURE = Path(__file__).parent / "fixtures" / "ont_hg002_phasing"
CORE_TOOLS = {tool: shutil.which(tool) for tool in ("samtools", "bcftools", "tabix")}


def _run(tool: str, *argv: str) -> str:
    return subprocess.run(
        [get_tool_path(tool), *argv], capture_output=True, text=True, check=True
    ).stdout


def _truth_haplotypes() -> dict[int, str]:
    """Position: the GIAB phased genotype, for the 0|1 and 1|0 sites."""
    rows = (
        line.split("\t")
        for line in (FIXTURE / "truth_phased.vcf").read_text().splitlines()
        if not line.startswith("#")
    )
    return {int(r[1]): r[9] for r in rows if r[9] in ("0|1", "1|0")}


def _write_calls(path: Path, samples: tuple[str, ...] = ("HG002",)) -> Path:
    """The truth's genotypes, unphased, as the sample's calls."""
    lines = []
    for line in (FIXTURE / "truth_phased.vcf").read_text().splitlines():
        if line.startswith("##"):
            lines.append(line)
            continue
        fields = line.split("\t")
        if line.startswith("#"):
            lines.append("\t".join(fields[:9] + list(samples)))
            continue
        genotype = fields[9].replace("|", "/")
        lines.append("\t".join(fields[:9] + [genotype] * len(samples)))
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture(scope="module")
def aligned(tmp_path_factory) -> tuple[Path, Path]:
    """(BAM, reference): the fixture reads through `wgsextract align`."""
    tmp = tmp_path_factory.mktemp("ont_phasing")
    ref = tmp / "ref.fa"
    shutil.copy(FIXTURE / "ref.fa", ref)
    out = tmp / "aligned"
    run_wgsextract(
        "align", "--r1", str(FIXTURE / "reads.fastq.gz"), "--ref", str(ref),
        "--outdir", str(out), "--sample", "HG002", "--threads", "2",
    )  # fmt: skip
    (bam,) = out.glob("*.bam")
    return bam, ref


def _phase(aligned, tmp_path: Path, **params) -> CommandResult:
    bam, ref = aligned
    if "vcf_input" not in params:
        params["vcf_input"] = _write_calls(tmp_path / "calls.vcf")
    return api.vcf.phase(
        api.VcfPhaseParams(
            input=bam, ref=ref, outdir=tmp_path / "phased", threads=2, **params
        )
    )


def _phased(vcf: Path) -> dict[int, tuple[str, str]]:
    rows = _run("bcftools", "query", "-f", "%POS[\t%GT\t%PS]\n", str(vcf))
    return {
        int(pos): (gt, ps)
        for pos, gt, ps in (line.split("\t") for line in rows.splitlines())
    }


@needs_tools("minimap2")
@needs_htslib
@pytest.mark.parametrize(
    "phaser",
    [
        pytest.param("whatshap", marks=needs_tools("whatshap")),
        pytest.param("longphase", marks=needs_tools("longphase")),
    ],
)
def test_phaser_recovers_the_giab_haplotypes_and_tags_reads(aligned, tmp_path, phaser):
    result = _phase(aligned, tmp_path, phaser=phaser, haplotag=True)

    outputs = {record.role: Path(record.path) for record in result.outputs}
    phased = _phased(outputs["phased_variants"])
    truth = _truth_haplotypes()
    # Nearly every site in one block (the last one or two, near the window's
    # end, can lack linking reads), with GIAB's haplotypes or both swapped.
    calls = {pos: phased[pos] for pos in truth if phased[pos][1] != "."}
    assert len(calls) >= 0.9 * len(truth)
    assert {ps for _gt, ps in calls.values()} == {str(min(truth))}
    swapped = {"0|1": "1|0", "1|0": "0|1"}
    expected = [truth[pos] for pos in calls]
    assert [gt for gt, _ps in calls.values()] in (
        expected,
        [swapped[gt] for gt in expected],
    )
    assert result.metrics["phaser"] == phaser
    assert {r.role for r in result.inputs} >= {"input", "vcf_input"}
    assert result.metrics["phase_blocks"] == 1
    assert result.metrics["phased_variants"] == len(calls)
    blocks = outputs["phase_blocks"].read_text().splitlines()
    first, last = str(min(calls)), str(max(calls))
    assert blocks[1].split("\t")[:4] == ["chr20", first, first, last]

    tagged = _run("samtools", "view", str(outputs["haplotagged_alignments"]))
    haplotypes = {
        field for line in tagged.splitlines() for field in line.split("\t")[11:]
        if field.startswith("HP:i:")
    }  # fmt: skip
    assert haplotypes == {"HP:i:1", "HP:i:2"}
    assert Path(f"{outputs['haplotagged_alignments']}.bai").is_file()


@needs_tools("minimap2")
@needs_htslib
@pytest.mark.parametrize(
    "phaser",
    [
        pytest.param("whatshap", marks=needs_tools("whatshap")),
        pytest.param("longphase", marks=needs_tools("longphase")),
    ],
)
def test_a_cram_region_is_phased_and_tagged_as_cram(aligned, tmp_path, phaser):
    bam, ref = aligned
    cram = tmp_path / "reads.cram"
    _run("samtools", "view", "-C", "-T", str(ref), "-o", str(cram), str(bam))
    _run("samtools", "index", str(cram))

    result = _phase(
        (cram, ref), tmp_path, phaser=phaser, haplotag=True, region="chr20:1-6000"
    )

    outputs = {record.role: Path(record.path) for record in result.outputs}
    phased = _phased(outputs["phased_variants"])
    assert max(phased) <= 6000
    inside = {pos for pos in _truth_haplotypes() if pos <= 6000}
    linked = {pos for pos, (_gt, ps) in phased.items() if ps != "."}
    assert linked <= inside and len(linked) >= 0.9 * len(inside)
    tagged = outputs["haplotagged_alignments"]
    assert tagged.name == "haplotagged.cram"
    reads = _run("samtools", "view", "-T", str(ref), str(tagged))
    assert "\tHP:i:1" in reads and "\tHP:i:2" in reads


@needs_tools("longphase", "minimap2")
@needs_htslib
def test_nanopore_reads_are_phased_with_longphase_by_default(aligned, tmp_path):
    result = _phase(aligned, tmp_path)
    assert result.metrics["phaser"] == "longphase"


@needs_tools("whatshap", "minimap2")
@needs_htslib
def test_the_reads_sample_is_phased_in_a_multi_sample_vcf(aligned, tmp_path):
    calls = _write_calls(tmp_path / "calls.vcf", ("HG001", "HG002"))
    result = _phase(aligned, tmp_path, vcf_input=calls, phaser="whatshap")
    (vcf,) = (r.path for r in result.outputs if r.role == "phased_variants")
    assert _run("bcftools", "query", "-l", vcf).split() == ["HG002"]

    other = _write_calls(tmp_path / "other.vcf", ("HG001", "HG003"))
    with pytest.raises(api.CommandFailed, match="HG001, HG003.*HG002"):
        _phase(aligned, tmp_path, vcf_input=other, phaser="whatshap")


@needs_tools("whatshap", "minimap2")
@needs_htslib
def test_the_api_phases_the_configured_default_vcf(aligned, tmp_path, monkeypatch):
    # #464 review: the API ignored default_input_vcf, unlike the CLI.
    calls = _write_calls(tmp_path / "configured.vcf")
    monkeypatch.setitem(settings, "default_input_vcf", str(calls))
    bam, ref = aligned
    result = api.vcf.phase(
        api.VcfPhaseParams(
            input=bam, ref=ref, outdir=tmp_path / "out", phaser="whatshap"
        )
    )
    assert str(calls) in {r.path for r in result.inputs if r.role == "vcf_input"}
    assert result.metrics["phased_variants"] > 0


@needs_tools("whatshap", "minimap2")
@needs_htslib
def test_short_reads_get_a_warning_and_no_longphase(aligned, tmp_path):
    bam, ref = aligned
    header = _run("samtools", "view", "-H", str(bam)).replace("PL:ONT", "PL:ILLUMINA")
    (tmp_path / "header.sam").write_text(header)
    illumina = tmp_path / "illumina.bam"
    with illumina.open("wb") as out:
        subprocess.run(
            [get_tool_path("samtools"), "reheader", str(tmp_path / "header.sam"),
             str(bam)],
            stdout=out, check=True,
        )  # fmt: skip
    _run("samtools", "index", str(illumina))

    if get_tool_path("longphase"):
        with pytest.raises(api.CommandFailed, match="LongPhase phases Nanopore or"):
            _phase((illumina, ref), tmp_path, phaser="longphase")
    result = _phase((illumina, ref), tmp_path)
    assert result.metrics["phaser"] == "whatshap"
    assert [w.code for w in result.warnings] == ["short_read_phasing"]


@pytest.mark.skipif(
    sys.platform == "win32", reason="a batch file can't take a multi-line argument"
)
def test_missing_phaser_names_its_pixi_environment(fake_pixi, monkeypatch):
    # The host has the core tools, and the lockfile has the phasers.
    for tool, found in CORE_TOOLS.items():
        if not found:
            pytest.skip(f"{tool} is not on PATH")
        (fake_pixi.host_bin / tool).symlink_to(found)
    monkeypatch.setattr(sys, "prefix", str(fake_pixi.env("default")))
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "default")
    fake_pixi.env("phasing", locked=("whatshap", "longphase"), installed=False)
    clear_tool_caches()

    with pytest.raises(MissingDependencyError, match=r"whatshap.*-e phasing"):
        run_wgsextract(
            "vcf", "phase", "--input", "reads.bam", "--vcf-input", "calls.vcf.gz"
        )


def test_windows_is_refused_before_inputs_are_resolved(monkeypatch):
    args = build_parser()[0].parse_args(
        ["vcf", "phase", "--input", "reads.bam", "--vcf-input", "calls.vcf.gz"]
    )
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(WGSExtractError, match="not available on Windows.*WSL2"):
        verify_platform_support(args)


@pytest.mark.skipif(sys.platform == "win32", reason="vcf phase is refused first")
def test_a_vcf_is_required(tmp_path):
    with pytest.raises(UsageError, match="--vcf-input"):
        run_wgsextract("vcf", "phase", "--input", str(tmp_path / "reads.bam"))


def _reheadered(bam: Path, out: Path, edit) -> Path:
    header = out.with_suffix(".header.sam")
    header.write_text(edit(_run("samtools", "view", "-H", str(bam))))
    with out.open("wb") as stream:
        subprocess.run(
            [get_tool_path("samtools"), "reheader", str(header), str(bam)],
            stdout=stream, check=True,
        )  # fmt: skip
    _run("samtools", "index", str(out))
    return out


@needs_tools("whatshap", "minimap2")
@needs_htslib
def test_reads_of_several_samples_are_refused(aligned, tmp_path):
    # #464 review: the phasers ignore read groups, so another sample's reads
    # would be phased in with this one's.
    bam, ref = aligned
    mixed = _reheadered(
        bam, tmp_path / "mixed.bam", lambda h: h + "@RG\tID:other\tSM:HG003\n"
    )
    with pytest.raises(api.CommandFailed, match="several samples.*HG002, HG003"):
        _phase((mixed, ref), tmp_path, phaser="whatshap")


HLA = "HLA-A*01:01:01:01"


@pytest.fixture(scope="module")
def two_contigs(aligned, tmp_path_factory) -> tuple[Path, Path]:
    """(BAM, reference): the reads split between chr20 and an hs38DH-style
    HLA contig holding the same sequence, as a two-contig genome."""
    bam, ref = aligned
    tmp = tmp_path_factory.mktemp("two_contigs")
    sequence = "".join(ref.read_text().splitlines()[1:])
    ref2 = tmp / "ref.fa"
    ref2.write_text(f">chr20\n{sequence}\n>{HLA}\n{sequence}\n")
    sam = _run("samtools", "view", "-h", "--no-PG", str(bam)).splitlines()
    lines = []
    for i, line in enumerate(sam):
        if line.startswith("@SQ"):
            line += f"\n@SQ\tSN:{HLA}\tLN:{len(sequence)}"
        elif not line.startswith("@") and i % 2:
            fields = line.split("\t")
            fields[2] = HLA
            line = "\t".join(f for f in fields if not f.startswith("SA:Z:"))
        lines.append(line)
    (tmp / "reads.sam").write_text("\n".join(lines) + "\n")
    out = tmp / "reads.bam"
    _run("samtools", "sort", "-o", str(out), str(tmp / "reads.sam"))
    _run("samtools", "index", str(out))
    return out, ref2


@needs_tools("whatshap", "minimap2")
@needs_htslib
def test_whatshap_phases_contigs_with_colons_in_parallel(two_contigs, tmp_path):
    # #464 review: `bcftools view -r` split "HLA-A*01:01:01:01" at the colons.
    calls = _write_calls(tmp_path / "chr20.vcf")
    text = calls.read_text().replace(
        "##contig=<ID=chr20,length=12000>",
        f"##contig=<ID=chr20,length=12000>\n##contig=<ID={HLA},length=12000>",
    )
    rows = [line for line in text.splitlines() if line.startswith("chr20\t")]
    both = tmp_path / "both.vcf"
    both.write_text(text + "".join(f"{HLA}{r[5:]}\n" for r in rows))

    result = _phase(two_contigs, tmp_path, vcf_input=both, phaser="whatshap")

    (vcf,) = (r.path for r in result.outputs if r.role == "phased_variants")
    phased = _run("bcftools", "query", "-i", 'GT~"|"', "-f", "%CHROM\n", vcf)
    assert set(phased.split()) == {"chr20", HLA}


@needs_tools("minimap2")
@needs_htslib
@pytest.mark.parametrize(
    "phaser",
    [
        pytest.param("whatshap", marks=needs_tools("whatshap")),
        pytest.param("longphase", marks=needs_tools("longphase")),
    ],
)
def test_reads_on_a_contig_without_calls_are_kept_untagged(
    two_contigs, tmp_path, phaser
):
    # #464 review: WhatsHap haplotag failed on a read contig missing from the
    # VCF header (whole-genome reads, chr20-only calls).
    bam, _ref = two_contigs
    result = _phase(two_contigs, tmp_path, phaser=phaser, haplotag=True)

    (tagged,) = (r.path for r in result.outputs if r.role == "haplotagged_alignments")
    assert _run("samtools", "view", "-c", tagged, HLA) == _run(
        "samtools", "view", "-c", str(bam), HLA
    )
    assert "HP:i:" in _run("samtools", "view", tagged, "chr20")
    assert "HP:i:" not in _run("samtools", "view", tagged, HLA)


@needs_tools("minimap2")
@needs_htslib
@pytest.mark.parametrize(
    "phaser",
    [
        pytest.param("whatshap", marks=needs_tools("whatshap")),
        pytest.param("longphase", marks=needs_tools("longphase")),
    ],
)
def test_several_regions_are_tagged_and_nothing_else(aligned, tmp_path, phaser):
    # #464 review: several --region intervals broke WhatsHap's haplotag
    # arguments, and LongPhase tagged the whole file.
    bam, ref = aligned
    regions = ["chr20:2000-2500", "chr20:9000-9500"]

    result = _phase(
        aligned, tmp_path, phaser=phaser, haplotag=True, region=",".join(regions)
    )

    (tagged,) = (r.path for r in result.outputs if r.role == "haplotagged_alignments")
    names = set(_run("samtools", "view", tagged).split("\n")) - {""}
    overlapping = _run("samtools", "view", "-M", str(bam), *regions)
    assert {n.split("\t")[0] for n in names} == {
        line.split("\t")[0] for line in overlapping.splitlines()
    }
