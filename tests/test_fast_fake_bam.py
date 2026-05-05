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

    class FakeStderr:
        def read(self) -> bytes:
            return b""

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stderr = FakeStderr()

        def wait(self) -> int:
            return 0

        def poll(self) -> int:
            return 0

        def kill(self):
            raise AssertionError("kill should not be called on success")

    def fake_popen(cmd, stdin, stderr):
        process = FakeProcess()
        captured["cmd"] = cmd
        captured["stdin_arg"] = stdin
        captured["stderr_arg"] = stderr
        captured["process"] = process
        return process

    def noise(_chrom_idx: int, pos: int, length: int) -> str:
        source = "ACGT" * ((pos + length + 4) // 4)
        return source[pos % 4 : pos % 4 + length]

    monkeypatch.setattr(qc.subprocess, "Popen", fake_popen)
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
        "-b",
        "-l",
        "1",
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
