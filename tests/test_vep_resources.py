import gzip

from wgsextract_cli.commands._vep_resources import preprocess_vcf_chr_prefix


def test_preprocess_vcf_chr_prefix_keeps_mito_header_and_records_consistent(tmp_path):
    input_vcf = tmp_path / "input.vcf"
    output_vcf = tmp_path / "output.vcf"
    input_vcf.write_text(
        "##fileformat=VCFv4.2\n"
        "##contig=<ID=1,length=1000>\n"
        "##contig=<ID=MT,length=16569>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "1\t10\trs1\tA\tG\t.\tPASS\t.\n"
        "MT\t20\trsMT\tC\tT\t.\tPASS\t.\n"
        "chrMT\t30\trsChrMT\tG\tA\t.\tPASS\t.\n"
    )

    preprocess_vcf_chr_prefix(str(input_vcf), str(output_vcf))

    assert output_vcf.read_text().splitlines() == [
        "##fileformat=VCFv4.2",
        "##contig=<ID=chr1,length=1000>",
        "##contig=<ID=chrM,length=16569>",
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
        "chr1\t10\trs1\tA\tG\t.\tPASS\t.",
        "chrM\t20\trsMT\tC\tT\t.\tPASS\t.",
        "chrM\t30\trsChrMT\tG\tA\t.\tPASS\t.",
    ]


def test_preprocess_vcf_chr_prefix_reads_gzip_input(tmp_path):
    input_vcf = tmp_path / "input.vcf.gz"
    output_vcf = tmp_path / "output.vcf"
    with gzip.open(input_vcf, "wt") as f:
        f.write(
            "##contig=<ID=M,length=16569>\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "M\t20\trsM\tC\tT\t.\tPASS\t.\n"
        )

    preprocess_vcf_chr_prefix(str(input_vcf), str(output_vcf))

    assert output_vcf.read_text().splitlines() == [
        "##contig=<ID=chrM,length=16569>",
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
        "chrM\t20\trsM\tC\tT\t.\tPASS\t.",
    ]
