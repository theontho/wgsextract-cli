from argparse import Namespace

from wgsextract_cli.commands import repair


def test_repair_vcf_file_mode_writes_repaired_vcf(tmp_path):
    input_vcf = tmp_path / "ftdna.vcf"
    input_vcf.write_text(
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr1\t100\t.\tA\tG\t100\tDP=1\t.\n",
        encoding="utf-8",
    )

    repair.repair_vcf(
        Namespace(
            input=str(input_vcf),
            outdir=str(tmp_path),
            output=None,
            _explicit_dests={"input"},
        )
    )

    output = tmp_path / "ftdna_repaired.vcf"
    assert "DP1" in output.read_text(encoding="utf-8")


def test_repair_vcf_file_mode_defaults_to_input_directory(tmp_path):
    input_vcf = tmp_path / "ftdna.vcf"
    input_vcf.write_text(
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr1\t100\t.\tA\tG\t100\tDP=1\t.\n",
        encoding="utf-8",
    )

    repair.repair_vcf(
        Namespace(
            input=str(input_vcf), outdir=None, output=None, _explicit_dests={"input"}
        )
    )

    assert "DP1" in (tmp_path / "ftdna_repaired.vcf").read_text(encoding="utf-8")


def test_repair_vcf_config_default_input_keeps_stream_mode(tmp_path, monkeypatch):
    input_vcf = tmp_path / "configured-default.vcf"
    input_vcf.write_text("##fileformat=VCFv4.2\n", encoding="utf-8")
    called = {}

    def fake_repair_vcf_stream(input_stream, output_stream):
        called["stream"] = True

    monkeypatch.setattr(repair, "repair_vcf_stream", fake_repair_vcf_stream)

    repair.repair_vcf(
        Namespace(input=str(input_vcf), output=None, _explicit_dests=set())
    )

    assert called == {"stream": True}
