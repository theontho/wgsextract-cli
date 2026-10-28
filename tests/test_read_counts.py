import gzip
import random
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize("compressed", [False, True])
def test_small_fastqs_count_both_mates_exactly(tmp_path, compressed):
    pair = [tmp_path / f"r{i}.fq" for i in (1, 2)]
    data = _fastq(123)
    for path in pair:
        path.write_bytes(gzip.compress(data) if compressed else data)
    assert read_counts.fastq_input_reads(*pair) == 246
    assert read_counts.fastq_input_reads(pair[0]) == 123


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
    small = read_counts.fastq_read_count(path)
    assert small == pytest.approx(800, rel=0.02)
    path.write_bytes(member * 40)
    large = read_counts.fastq_read_count(path)
    assert large == pytest.approx(8000, rel=0.02)
    assert large == pytest.approx(small * 10, abs=10)


def test_sample_respects_compressed_byte_limit(tmp_path, monkeypatch):
    member = gzip.compress(_fastq(200))
    path = tmp_path / "reads.gz"
    path.write_bytes(member * 20)
    monkeypatch.setattr(read_counts, "_COMPRESSED_SAMPLE_BYTES", len(member) * 2)
    assert read_counts.fastq_read_count(path) == 4000


def test_gzip_read_ahead_does_not_bias_highly_compressed_reads(tmp_path, monkeypatch):
    data = b"@r\nACGT\n+\nIIII\n" * 10000
    member = gzip.compress(data)
    path = tmp_path / "repeats.gz"
    path.write_bytes(member * 40)
    monkeypatch.setattr(read_counts, "_DECODED_SAMPLE_BYTES", len(data) * 3)
    assert read_counts.fastq_read_count(path) == pytest.approx(400000, rel=0.02)


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
    assert read_counts.fastq_read_count(tmp_path / "missing.fq") is None


def test_streams_are_not_opened_or_consumed_for_sampling(tmp_path, monkeypatch):
    monkeypatch.setattr(
        read_counts.Path, "stat", lambda _: SimpleNamespace(st_mode=0, st_size=0)
    )
    monkeypatch.setattr(
        read_counts, "open", lambda *_a: pytest.fail("opened a stream"), raising=False
    )
    assert read_counts.fastq_read_count(tmp_path / "pipe") is None
