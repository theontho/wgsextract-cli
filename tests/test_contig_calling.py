import gzip
import logging
import random
import re
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

from tests.htslib_helpers import needs_htslib, run_wgsextract, sam_to_bam, write_fasta
from wgsextract_cli.commands.info import metrics
from wgsextract_cli.commands.vcf import contig_calling
from wgsextract_cli.core.utils import WGSExtractError, run_command, run_pipeline


@pytest.fixture(scope="module")
def multicontig_sample(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    root = tmp_path_factory.mktemp("contig calls")
    rng = random.Random(11)
    contigs = ["chr20", "chr21", "empty", "chrX", "chrY", "chrM", "HLA-A*01:01:01:01"]
    sequences = {
        contig: "".join(rng.choice("ACGT") for _ in range(1000)) for contig in contigs
    }
    sequences = {name: seq[:499] + "A" + seq[500:] for name, seq in sequences.items()}
    sequences = {
        name: seq[:698] + "ACGTCA" + seq[704:] for name, seq in sequences.items()
    }
    ref = write_fasta(
        root / "reference.fa", {**sequences, "reference_only": "A" * 1000}
    )
    lines = ["@HD\tVN:1.6\tSO:coordinate"]
    lines += [f"@SQ\tSN:{name}\tLN:1000" for name in contigs]
    lines += ["@RG\tID:reads\tSM:sample"]
    for chrom, seq in sequences.items():
        if chrom == "empty":
            continue
        for variant, first in (("snp", 440), ("del", 650)):
            for i in range(24):
                start = first + 2 * i
                bases, cigar = seq[start - 1 : start + 99], "100M"
                alt = i % 2 == 0 or chrom in {"chrX", "chrY", "chrM"}
                if alt and variant == "snp":
                    k = 500 - start
                    bases = bases[:k] + "G" + bases[k + 1 :]
                elif alt:
                    left = 700 - start + 1
                    cigar = f"{left}M2D{100 - left}M"
                    bases = seq[start - 1 : 700] + seq[702 : start + 101]
                lines.append(
                    f"{chrom}_{variant}_{i}\t0\t{chrom}\t{start}\t60\t{cigar}"
                    f"\t*\t0\t0\t{bases}\t{'I' * len(bases)}\tRG:Z:reads"
                )
    return sam_to_bam(lines, root / "sample.bam"), str(ref)


def _call(
    sample: tuple[str, str], output: Path, kind: str, threads: int, *extra: str
) -> str:
    bam, ref = sample
    run_wgsextract(
        "vcf", kind, "--input", bam, "--ref", ref, "--ploidy", "GRCh38",
        "--threads", str(threads), "--outdir", str(output), *extra,
    )  # fmt: skip
    return str(output / f"{kind}s.vcf.gz")


@needs_htslib
@pytest.mark.parametrize("kind", ["snp", "indel"])
@pytest.mark.parametrize("sex", ["Male", "Female"])
@pytest.mark.parametrize("fmt", ["bam", "cram"])
def test_parallel_calls_match_serial_body_and_headers(
    multicontig_sample: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sex: str,
    kind: str,
    fmt: str,
) -> None:
    infer_sex = Mock(return_value=sex)
    monkeypatch.setattr(metrics, "sample_sex", infer_sex)
    bam, ref = multicontig_sample
    if fmt == "cram":
        cram = str(tmp_path / "sample.cram")
        run_command(["samtools", "view", "-C", "-T", ref, "-o", cram, bam])
        run_command(["samtools", "index", cram])
        bam = cram
    serial = _call((bam, ref), tmp_path / "serial", kind, 1)
    parallel = _call((bam, ref), tmp_path / "parallel", kind, 3, "--parallel")
    bodies = [
        run_command(["bcftools", "view", "-H", path], capture_output=True).stdout
        for path in (serial, parallel)
    ]
    assert bodies[0] == bodies[1]
    assert "chr20\t" in bodies[0] and "HLA-A*01:01:01:01\t" in bodies[0]
    assert ("chrY\t" in bodies[0]) == (sex == "Male")
    headers = [
        run_command(["bcftools", "view", "-h", path], capture_output=True).stdout
        for path in (serial, parallel)
    ]
    relevant = ("#CHROM", "##contig=", "##INFO=", "##FORMAT=")
    assert [line for line in headers[0].splitlines() if line.startswith(relevant)] == [
        line for line in headers[1].splitlines() if line.startswith(relevant)
    ]
    assert infer_sex.call_count == 2  # Once per command, not once per worker.
    assert Path(parallel + ".tbi").is_file()
    assert not list((tmp_path / "parallel").glob(".wgse_call_*"))


@pytest.fixture(scope="module")
def deep_sample(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """Two contigs covered by 300 reads, so bcftools subsamples their sites."""
    root = tmp_path_factory.mktemp("deep calls")
    rng = random.Random(7)
    sequences = {
        name: "".join(rng.choice("ACGT") for _ in range(1000))
        for name in ("chrA", "chrB")
    }
    ref = write_fasta(root / "reference.fa", sequences)
    lines = ["@HD\tVN:1.6\tSO:coordinate"]
    lines += [f"@SQ\tSN:{name}\tLN:1000" for name in sequences]
    for chrom, seq in sequences.items():
        for start in range(1, 701):
            bases = list(seq[start - 1 : start + 299])
            if 201 <= start <= 500 and rng.random() < 0.5:
                bases[500 - start] = "ACGT"[("ACGT".index(seq[499]) + 1) % 4]
            quals = "".join(chr(33 + rng.randint(15, 40)) for _ in bases)
            lines.append(
                f"r{chrom}{start}\t0\t{chrom}\t{start}\t60\t300M\t*\t0\t0"
                f"\t{''.join(bases)}\t{quals}"
            )
    return sam_to_bam(lines, root / "sample.bam"), str(ref)


def _bodies(
    sample: tuple[str, str], tmp_path: Path, *, indels: bool = False
) -> list[str]:
    bam, ref = sample
    bodies = []
    for threads in ("1", "3"):
        output = str(tmp_path / f"calls-{threads}.vcf.gz")
        contig_calling.call_small_variants(
            bam, ref, output, threads=threads, region=None, profile=[],
            call_args=["--ploidy", "2", "-P", "0"], indels=indels, parallel=True,
        )  # fmt: skip
        bodies.append(
            run_command(["bcftools", "view", "-H", output], capture_output=True).stdout
        )
    assert not list(tmp_path.glob(".wgse_call_*"))
    return bodies


@needs_htslib
def test_parallel_deep_calls_match_one_stream(
    deep_sample: tuple[str, str], tmp_path: Path
) -> None:
    # bcftools draws from one per-process random stream to subsample sites
    # deeper than 255 bases, so per-contig workers would change chrB (#392).
    serial, parallel = _bodies(deep_sample, tmp_path)
    assert serial == parallel
    assert "chrB\t500\t" in serial


def _deep_patches(
    root: Path, *, long_reads: bool = False, samples: int = 1
) -> tuple[str, str]:
    """Shallow contigs with het SNPs and a het deletion, two of which hold
    300-read deep patches. ``long_reads`` adds a 300-read pile at chrA 2,000
    whose last 50 reads span 50 kb, past the deep-site windows' lead-in."""
    rng = random.Random(5)
    length = 60_000 if long_reads else 30_000
    patches = {"chrA": [3_000, 25_000], "chrB": [], "chrC": [15_000]}
    if long_reads:
        patches["chrA"] = [45_000]
    sequences = {
        name: "".join(rng.choice("ACGT") for _ in range(length)) for name in patches
    }
    ref = write_fasta(root / "reference.fa", sequences)
    lines = ["@HD\tVN:1.6\tSO:coordinate"]
    lines += [f"@SQ\tSN:{name}\tLN:{length}" for name in sequences]
    lines += [f"@RG\tID:{i}\tSM:sample{i}" for i in range(samples)]
    for chrom, seq in sequences.items():
        snps = set(range(500, length, 1_000)) | {p + 450 for p in patches[chrom]}
        reads = [(start, 100) for start in range(1, length - 99, 10)]
        reads += [
            (start, 300)
            for patch in patches[chrom]
            for start in range(patch, patch + 600)
        ]
        if long_reads and chrom == "chrA":
            reads += [(start, 300) for start in range(2_000, 2_300)]
            reads += [(start, 50_000) for start in range(2_250, 2_300)]
        for name, (start, size) in enumerate(sorted(reads)):
            bases = list(seq[start - 1 : start - 1 + size])
            cigar = f"{size}M"
            for snp in snps:
                if start <= snp < start + size and rng.random() < 0.5:
                    bases[snp - start] = "ACGT"[("ACGT".index(seq[snp - 1]) + 1) % 4]
            if size == 100 and 210 < start < 290 and rng.random() < 0.5:
                # Deletes the two bases after 300, before any SNP.
                left = 300 - start + 1
                bases = list(seq[start - 1 : 300] + seq[302 : start + 101])
                cigar = f"{left}M2D{100 - left}M"
            quals = "".join(chr(33 + rng.randint(15, 40)) for _ in bases)
            lines.append(
                f"r{chrom}{name}\t0\t{chrom}\t{start}\t60\t{cigar}\t*\t0\t0"
                f"\t{''.join(bases)}\t{quals}\tRG:Z:{name % samples}"
            )
    return sam_to_bam(lines, root / "sample.bam"), str(ref)


@pytest.fixture(scope="module")
def deep_patches_sample(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    return _deep_patches(tmp_path_factory.mktemp("deep patches"))


@needs_htslib
def test_parallel_replays_only_deep_windows(
    deep_patches_sample: tuple[str, str],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # #392: per-contig workers changed QUAL/PL at sites deeper than 255 reads.
    caplog.set_level(logging.INFO)
    serial, parallel = _bodies(deep_patches_sample, tmp_path)
    assert serial == parallel
    assert "chrC\t15450\t" in serial and "chrB\t500\t" in serial
    # Only the stretches around chrA's and chrC's patches are recalled, not
    # the 60 kb of both contigs.
    replays = re.findall(r"Calling (\d+) deep stretches \(([\d,]+) bp\)", caplog.text)
    assert len(replays) == 1
    count, bases = replays[0]
    assert count == "3" and int(bases.replace(",", "")) < 40_000
    assert "disagreed" not in caplog.text


@needs_htslib
def test_reads_longer_than_the_lead_in_fall_back_to_whole_contigs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Reads that start before a window's lead-in reach its deep patch, so the
    # window cannot reproduce which of them mpileup's read cap kept.
    caplog.set_level(logging.INFO)
    sample = _deep_patches(tmp_path, long_reads=True)
    serial, parallel = _bodies(sample, tmp_path)
    assert serial == parallel
    assert "chrA\t45450\t" in serial and "chrC\t15450\t" in serial
    assert "replaying the 2 contigs with deep sites whole" in caplog.text


@needs_htslib
@pytest.mark.parametrize("indels", [False, True])
def test_several_samples_are_called_in_one_stream(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, indels: bool
) -> None:
    # With several samples, indel calls draw too and gVCF bands hide deep
    # columns, so --parallel calls them serially.
    caplog.set_level(logging.INFO)
    sample = _deep_patches(tmp_path, samples=2)
    serial, parallel = _bodies(sample, tmp_path, indels=indels)
    assert serial == parallel
    assert ("chrA\t299\t" if indels else "chrC\t15450\t") in serial
    assert "Calling serially: with several samples" in caplog.text
    assert "contigs with" not in caplog.text


@needs_htslib
def test_parallel_deep_indel_calls_match_one_stream(
    deep_patches_sample: tuple[str, str], tmp_path: Path
) -> None:
    # One sample's indel likelihoods never subsample, so no replay is needed.
    # Each contig's deletion precedes its SNPs, where one stream used to carry
    # the previous contig's INFO/BQBZ and INFO/MQSBZ into the record (#392).
    serial, parallel = _bodies(deep_patches_sample, tmp_path, indels=True)
    assert serial == parallel
    assert [line.split("\t")[0] for line in serial.splitlines()] == [
        "chrA",
        "chrB",
        "chrC",
    ]


@needs_htslib
def test_iupac_indels_are_valid_without_changing_other_contigs(
    multicontig_sample: tuple[str, str], tmp_path: Path
) -> None:
    bam, original_ref = multicontig_sample
    sequences = {
        entry.splitlines()[0]: "".join(entry.splitlines()[1:])
        for entry in Path(original_ref).read_text().split(">")[1:]
    }
    sequence = sequences["chr20"]
    sequences["chr20"] = sequence[:699] + "YR" + sequence[701:]
    ref = str(write_fasta(tmp_path / "iupac.fa", sequences))
    call_args = ["--ploidy", "2", "-P", "0"]
    bodies = []
    for threads in ("1", "3"):
        output = str(tmp_path / f"indels-{threads}.vcf.gz")
        contig_calling.call_small_variants(
            bam, ref, output, threads=threads, region=None,
            profile=[], call_args=call_args, indels=True, parallel=True,
        )  # fmt: skip
        bodies.append(
            run_command(["bcftools", "view", "-H", output], capture_output=True).stdout
        )
    assert bodies[0] == bodies[1]
    body = bodies[0]
    records = [line.split("\t") for line in body.splitlines()]
    assert all(
        set(allele) <= set("ACGTN")
        for record in records
        for allele in (record[3], *record[4].split(","))
    )
    affected = [record for record in records if record[0] == "chr20"]
    assert len(affected) == 1
    assert affected[0][1:5] == ["700", ".", "CAT", "C"]

    consensus_dir = tmp_path / "consensus"
    run_wgsextract(
        "vcf", "to-fasta", "--input", output, "--ref", ref,
        "--region", "chr20:695-710", "--haplotype", "alt",
        "--outdir", str(consensus_dir),
    )  # fmt: skip
    with gzip.open(next(consensus_dir.glob("*_consensus.fa.gz")), "rt") as source:
        consensus = "".join(source.read().splitlines()[1:])
    expected = sequences["chr20"][694:699] + "C" + sequences["chr20"][702:710]
    assert consensus == expected
    assert (
        run_command(["bcftools", "view", "-H", output], capture_output=True).stdout
        == body
    )

    baseline = str(tmp_path / "baseline.vcf.gz")
    stages = contig_calling._pipeline(
        bam, original_ref, baseline, threads="1", region_args=[],
        profile=[], call_args=call_args, indels=True,
    )  # fmt: skip
    stages[-1] = ["bcftools", "norm", "-f", original_ref, "-Oz", "-o", baseline]
    run_pipeline(stages)
    original_body = run_command(
        ["bcftools", "view", "-H", baseline], capture_output=True
    ).stdout
    assert [line for line in body.splitlines() if not line.startswith("chr20\t")] == [
        line for line in original_body.splitlines() if not line.startswith("chr20\t")
    ]


@needs_htslib
def test_worker_failure_cleans_parts_without_concatenating(
    multicontig_sample: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = Mock(side_effect=WGSExtractError("worker failed"))
    concat = Mock(side_effect=AssertionError("must not concatenate failed parts"))
    monkeypatch.setattr(contig_calling, "run_pipeline", pipeline)
    monkeypatch.setattr(contig_calling, "run_command", concat)
    bam, ref = multicontig_sample
    with pytest.raises(WGSExtractError, match="worker failed"):
        contig_calling.call_small_variants(
            bam, ref, str(tmp_path / "result.vcf.gz"), threads="3", region=None,
            profile=[], call_args=["--ploidy", "2"], parallel=True,
        )  # fmt: skip
    concat.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_worker_failure_cancels_running_sibling_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, stopped = Event(), Event()
    monkeypatch.setattr(
        contig_calling,
        "_parallel_contigs",
        Mock(return_value={"chr20": 1000, "chr21": 1000}),
    )

    monkeypatch.setattr(
        contig_calling, "get_bam_header", Mock(return_value="@RG\tID:a\tSM:one\n")
    )

    def pipeline(
        stages: list[list[str]], *, description: str, cancel_event: Event, **_: object
    ) -> None:
        if description.endswith("(chr20)"):
            started.set()
            assert cancel_event.wait(5), "running worker was not cancelled"
            assert list(tmp_path.glob(".wgse_call_*"))
            stopped.set()
        else:
            assert started.wait(5)
            raise WGSExtractError("original worker failure")

    monkeypatch.setattr(contig_calling, "run_pipeline", pipeline)
    with pytest.raises(WGSExtractError, match="original worker failure"):
        contig_calling.call_small_variants(
            "input.bam", "ref.fa", str(tmp_path / "result.vcf.gz"),
            threads="2", region=None, profile=[], call_args=[], parallel=True,
        )  # fmt: skip
    assert stopped.is_set()
    assert list(tmp_path.iterdir()) == []


@needs_htslib
def test_concat_failure_preserves_previous_output(
    multicontig_sample: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "result.vcf.gz"
    output.write_bytes(b"previous output")
    index = Path(str(output) + ".tbi")
    index.write_bytes(b"previous index")

    def fail_concat(command: list[str]) -> None:
        Path(command[-1]).write_bytes(b"partial output")
        raise WGSExtractError("concat failed")

    monkeypatch.setattr(contig_calling, "run_pipeline", Mock())
    monkeypatch.setattr(contig_calling, "run_command", fail_concat)
    bam, ref = multicontig_sample
    with pytest.raises(WGSExtractError, match="concat failed"):
        contig_calling.call_small_variants(
            bam, ref, str(output), threads="3", region=None, profile=[],
            call_args=["--ploidy", "2"], indels=True, parallel=True,
        )  # fmt: skip
    assert output.read_bytes() == b"previous output"
    assert index.read_bytes() == b"previous index"
    assert sorted(tmp_path.iterdir()) == sorted([output, index])


@pytest.mark.parametrize("contents", ["-2\t1\t100\t0\t0\t0\n", "invalid\n"])
def test_unknown_cram_index_does_not_skip_any_contigs(
    contents: str, tmp_path: Path
) -> None:
    index = tmp_path / "input.cram.crai"
    with gzip.open(index, "wt") as stream:
        stream.write(contents)
    assert contig_calling._cram_contigs(str(index), ["chr20", "chr21"]) == {
        "chr20",
        "chr21",
    }
