import gzip
import random
from types import SimpleNamespace

import pytest

from tests.htslib_helpers import needs_tools, sam_to_bam
from wgsextract_cli.core import read_counts


def _fastq(reads=100):
    rng = random.Random(23)
    return b"".join(
        b"@read%d\n%s\n+\n%s\n"
        % (
            i,
            "".join(rng.choices("ACGT", k=150)).encode(),
            "".join(rng.choices("ABCDEFGHI", k=150)).encode(),
        )
        for i in range(reads)
    )


@needs_tools("samtools")
def test_alignment_length_samples_only_primary_pairs_and_stops_at_record_limit(
    tmp_path,
):
    records = ["@HD\tVN:1.6\tSO:unsorted"]
    for i in range(4100):
        length = 10 if i < 4096 else 1000
        records.append(f"r{i}\t77\t*\t0\t0\t*\t*\t0\t0\t{'A' * length}\t{'I' * length}")
    for flag in (4, 77 | 0x100, 77 | 0x800):
        records.insert(1, f"skip\t{flag}\t*\t0\t0\t*\t*\t0\t0\tAAAA\tIIII")
    source = sam_to_bam(records, tmp_path / "reads.bam")
    assert read_counts.alignment_read_length(source) == 10
    assert read_counts.alignment_read_length(tmp_path / "missing.bam") is None


@needs_tools("samtools")
def test_alignment_length_sampling_bounds_unpaired_input_scanning(tmp_path):
    records = ["@HD\tVN:1.6\tSO:unsorted"]
    records.extend(f"single{i}\t4\t*\t0\t0\t*\t*\t0\t0\tA\tI" for i in range(4096))
    records.append("tail\t77\t*\t0\t0\t*\t*\t0\t0\tAAAA\tIIII")
    source = sam_to_bam(records, tmp_path / "unpaired.bam")
    assert read_counts.alignment_read_length(source) is None


@pytest.mark.parametrize("compressed", [False, True])
def test_small_fastqs_count_both_mates_exactly(tmp_path, compressed):
    pair = [tmp_path / f"r{i}.fq" for i in (1, 2)]
    data = _fastq(123)
    for path in pair:
        path.write_bytes(gzip.compress(data) if compressed else data)
    assert read_counts.fastq_input_reads(*pair) == 246
    assert read_counts.fastq_input_reads(pair[0]) == 123


@pytest.mark.parametrize("compressed", [False, True])
def test_sampled_length_weights_different_mate_lengths_and_read_counts(
    tmp_path, compressed
):
    pair = [tmp_path / f"r{i}.fq" for i in (1, 2)]
    for path, count, length in zip(pair, (2, 3), (100, 300), strict=True):
        data = (b"@r\n" + b"A" * length + b"\n+\n" + b"I" * length + b"\n") * count
        path.write_bytes(gzip.compress(data) if compressed else data)
    assert read_counts.fastq_input_statistics(*pair) == read_counts.FastqStatistics(
        5, 220
    )


@pytest.mark.parametrize("compressed", [False, True])
def test_bounded_sample_extrapolates_file_size_including_gzip_members(
    tmp_path, monkeypatch, compressed
):
    data = _fastq(200)
    member = gzip.compress(data) if compressed else data
    monkeypatch.setattr(read_counts, "_DECODED_SAMPLE_BYTES", len(data) * 2 + 19)
    monkeypatch.setattr(read_counts, "_COMPRESSED_SAMPLE_BYTES", len(member) * 3)
    path = tmp_path / "reads.fq"
    path.write_bytes(member * 4)
    small = read_counts.fastq_statistics(path)
    assert small is not None
    assert small.reads == pytest.approx(800, rel=0.02)
    path.write_bytes(member * 40)
    large = read_counts.fastq_statistics(path)
    assert large is not None
    assert large.reads == pytest.approx(8000, rel=0.02)
    assert large.reads == pytest.approx(small.reads * 10, abs=10)


def test_sample_respects_compressed_byte_limit(tmp_path, monkeypatch):
    member = gzip.compress(_fastq(200))
    path = tmp_path / "reads.gz"
    path.write_bytes(member * 20)
    monkeypatch.setattr(read_counts, "_COMPRESSED_SAMPLE_BYTES", len(member) * 2)
    assert read_counts.fastq_statistics(path) == read_counts.FastqStatistics(4000, 150)


def test_gzip_read_ahead_does_not_bias_highly_compressed_reads(tmp_path, monkeypatch):
    data = b"@r\nACGT\n+\nIIII\n" * 10000
    member = gzip.compress(data)
    path = tmp_path / "repeats.gz"
    path.write_bytes(member * 40)
    monkeypatch.setattr(read_counts, "_DECODED_SAMPLE_BYTES", len(data) * 3)
    statistics = read_counts.fastq_statistics(path)
    assert statistics is not None
    assert statistics.reads == pytest.approx(400000, rel=0.02)


@pytest.mark.parametrize(
    "data",
    [b"", b"not fastq\n", b"@r\nA\n+\nII\n", gzip.compress(_fastq())[:-5]],
    ids=["empty", "not-fastq", "bad-quality", "truncated-gzip"],
)
def test_unreadable_or_invalid_samples_do_not_omit_a_mate(tmp_path, data, caplog):
    good, bad = tmp_path / "good.fq", tmp_path / "bad.fq"
    good.write_bytes(_fastq())
    bad.write_bytes(data)
    assert read_counts.fastq_input_reads(good, bad) is None
    assert "Could not estimate input read count" in caplog.text
    assert read_counts.fastq_statistics(tmp_path / "missing.fq") is None


def test_streams_are_not_opened_or_consumed_for_sampling(tmp_path, monkeypatch):
    monkeypatch.setattr(
        read_counts.Path, "stat", lambda _: SimpleNamespace(st_mode=0, st_size=0)
    )
    monkeypatch.setattr(
        read_counts, "open", lambda *_a: pytest.fail("opened a stream"), raising=False
    )
    assert read_counts.fastq_statistics(tmp_path / "pipe") is None
