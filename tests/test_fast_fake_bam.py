from io import StringIO

from wgsextract_cli.commands.qc import _stream_fast_bam_sam


def test_stream_fast_bam_sam_is_coordinate_sorted_and_human_like():
    output = StringIO()

    def noise(_chrom_idx: int, pos: int, length: int) -> str:
        source = "ACGT" * ((pos + length + 4) // 4)
        return source[pos % 4 : pos % 4 + length]

    _stream_fast_bam_sam(
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
