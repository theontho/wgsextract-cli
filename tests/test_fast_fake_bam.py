from argparse import Namespace
from io import StringIO

from wgsextract_cli.commands import qc


def test_stream_fast_bam_sam_is_coordinate_sorted_and_human_like():
    output = StringIO()

    def noise(_chrom_idx: int, pos: int, length: int) -> str:
        source = "ACGT" * ((pos + length + 4) // 4)
        return source[pos % 4 : pos % 4 + length]

    qc._stream_fast_bam_sam(
        output.write,
        {"chr1": 1200, "chr2": 900},
        coverage=4.0,
        seed=42,
        target_md5="abc123",
        get_noise_seq=noise,
    )

    lines = output.getvalue().splitlines()
    assert "@RG\tID:sample1\tSM:sample1\tPL:ILLUMINA\tDS:MD5:abc123" in lines
    assert "@CO\tMD5:abc123" in lines

    records = [line.split("\t") for line in lines if not line.startswith("@")]
    assert records
    assert {record[1] for record in records} == {"99", "147"}
    assert any("NM:i:1" in record[11:] for record in records)

    last_chrom = None
    last_pos = 0
    chrom_order = {"chr1": 0, "chr2": 1}
    for record in records:
        chrom = record[2]
        pos = int(record[3])
        cigar = record[5]
        seq = record[9]
        qual = record[10]

        assert cigar.endswith("M")
        assert len(seq) == int(cigar[:-1])
        assert len(qual) == len(seq)
        if chrom != last_chrom:
            if last_chrom is not None:
                assert chrom_order[chrom] > chrom_order[last_chrom]
            last_chrom = chrom
            last_pos = 0
        assert pos >= last_pos
        last_pos = pos


def test_stream_fast_bam_sam_applies_consistent_snp_variants():
    output = StringIO()

    qc._stream_fast_bam_sam(
        output.write,
        {"chr1": 2500},
        coverage=50.0,
        seed=42,
        target_md5=None,
        get_noise_seq=lambda _chrom_idx, _pos, length: "A" * length,
    )

    records = [
        line.split("\t")
        for line in output.getvalue().splitlines()
        if not line.startswith("@")
    ]
    variant_pos = qc._first_fast_bam_variant_pos(0, 42)
    variant_base = qc._fast_bam_alt_base("A", 0, variant_pos, 42)
    variant_covering_records = [
        record
        for record in records
        if int(record[3]) <= variant_pos < int(record[3]) + len(record[9])
    ]

    assert variant_covering_records
    for record in variant_covering_records:
        read_offset = variant_pos - int(record[3])
        assert record[9][read_offset] == variant_base
        assert "NM:i:1" in record[11:]


def test_reference_backed_sequence_provider_fetches_indexed_reference(
    monkeypatch, tmp_path
):
    ref = tmp_path / "ref.fa"
    ref.write_text(">chr1\nAACCGGTTAACCGGTT\n", encoding="utf-8")
    ref.with_suffix(".fa.fai").write_text("chr1\t16\t6\t16\t17\n", encoding="utf-8")
    calls = []

    def fake_run_command(cmd, capture_output=False, check=True):
        calls.append(cmd)
        region = cmd[-1]
        assert region == "chr1:1-16"

        class Result:
            returncode = 0
            stdout = ">chr1:1-16\nAACCGGTTAACCGGTT\n"

        return Result()

    monkeypatch.setattr(qc, "run_command", fake_run_command)
    provider = qc._reference_backed_sequence_provider(
        str(ref), {"chr1": 16}, lambda _chrom_idx, _pos, length: "N" * length
    )

    assert provider(0, 2, 4) == "CCGG"
    assert calls == [["samtools", "faidx", str(ref), "chr1:1-16"]]


def test_generate_fake_genomics_data_uses_streaming_bam_by_default(
    monkeypatch, tmp_path
):
    calls = []

    def fake_create_fast_fake_bam(
        bam_path,
        chroms,
        coverage,
        seed,
        target_md5,
        get_noise_seq,
        threads,
    ):
        calls.append(
            {
                "bam_path": bam_path,
                "chroms": chroms,
                "coverage": coverage,
                "seed": seed,
                "target_md5": target_md5,
                "sample": get_noise_seq(0, 0, 4),
                "threads": threads,
            }
        )

    monkeypatch.setattr(qc, "_create_fast_fake_bam", fake_create_fast_fake_bam)
    monkeypatch.setattr(
        qc,
        "_reference_backed_sequence_provider",
        lambda _ref_path, _chroms, fallback: fallback,
    )
    monkeypatch.setattr(
        qc, "get_resource_defaults", lambda _threads, _memory: ("2", None)
    )
    monkeypatch.setattr(qc, "run_command", lambda *args, **kwargs: None)

    qc.generate_fake_genomics_data(
        str(tmp_path),
        ref_path=None,
        coverage=1.0,
        seed=1,
        build="hg38",
        full_size=False,
        types=["bam"],
    )

    assert len(calls) == 1
    assert calls[0]["bam_path"] == str(tmp_path / "fake.bam")
    assert calls[0]["coverage"] == 1.0
    assert "chr1" in calls[0]["chroms"]


def test_cmd_fake_data_rejects_legacy_bam_with_full_size(monkeypatch, tmp_path):
    monkeypatch.setattr(qc, "verify_dependencies", lambda _tools: None)
    monkeypatch.setattr(qc, "log_dependency_info", lambda _tools: None)

    args = Namespace(
        outdir=str(tmp_path),
        coverage=1.0,
        seed=1,
        build="hg38",
        full_size=True,
        type="bam",
        ref=None,
        legacy_bam=True,
    )

    try:
        qc.cmd_fake_data(args)
    except qc.WGSExtractError as exc:
        assert "--legacy-bam" in str(exc)
    else:
        raise AssertionError("Expected --legacy-bam with --full-size to fail")


def test_full_size_bam_creates_explicit_reference_path(monkeypatch, tmp_path):
    calls = []
    ref_path = tmp_path / "benchmark_ref.fa"

    def fake_write_reference(path, chroms, get_noise_seq):
        calls.append({"path": path, "chroms": chroms, "sample": get_noise_seq(0, 0, 4)})
        ref_path.write_text(">chr1\nACGT\n", encoding="utf-8")

    monkeypatch.setattr(qc, "_write_fake_reference", fake_write_reference)
    monkeypatch.setattr(qc, "run_command", lambda *args, **kwargs: None)
    monkeypatch.setattr(qc, "_create_fast_fake_bam", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        qc,
        "_reference_backed_sequence_provider",
        lambda _ref_path, _chroms, fallback: fallback,
    )
    monkeypatch.setattr(
        qc, "get_resource_defaults", lambda _threads, _memory: ("2", None)
    )

    qc.generate_fake_genomics_data(
        str(tmp_path),
        ref_path=str(ref_path),
        coverage=0.01,
        seed=1,
        build="hg38",
        full_size=True,
        types=["bam"],
    )

    assert len(calls) == 1
    assert calls[0]["path"] == str(ref_path)
    assert calls[0]["chroms"]["chr1"] == 248956422


def test_create_fast_fake_bam_streams_sam_to_samtools(monkeypatch, tmp_path):
    captured = {}

    class FakeStdin:
        def __init__(self):
            self.data = bytearray()
            self.closed = False

        def write(self, data: bytes):
            self.data.extend(data)

        def close(self):
            self.closed = True

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeStdin()
            self._running = True

        def wait(self) -> int:
            self._running = False
            return 0

        def poll(self) -> int | None:
            return None if self._running else 0

        def kill(self):
            raise AssertionError("kill should not be called on success")

    def fake_popen(cmd, stdin):
        process = FakeProcess()
        captured["cmd"] = cmd
        captured["stdin_arg"] = stdin
        captured["process"] = process
        return process

    def noise(_chrom_idx: int, pos: int, length: int) -> str:
        source = "ACGT" * ((pos + length + 4) // 4)
        return source[pos % 4 : pos % 4 + length]

    monkeypatch.setattr(qc, "popen", fake_popen)
    monkeypatch.setattr(qc, "get_tool_path", lambda _tool: "samtools")

    qc._create_fast_fake_bam(
        str(tmp_path / "fake.bam"),
        {"chr1": 800},
        coverage=1.0,
        seed=7,
        target_md5=None,
        get_noise_seq=noise,
        threads="2",
    )

    assert captured["cmd"] == [
        "samtools",
        "view",
        "-@",
        "2",
        "-1",
        "-b",
        "-o",
        str(tmp_path / "fake.bam"),
        "-",
    ]
    process = captured["process"]
    assert process.stdin.closed
    sam_text = process.stdin.data.decode()
    assert sam_text.startswith("@HD\tVN:1.6\tSO:coordinate\n")
    assert "\n@SQ\tSN:chr1\tLN:800\n" in sam_text
    assert "\t99\tchr1\t" in sam_text
    assert "\t147\tchr1\t" in sam_text
