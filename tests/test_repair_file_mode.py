from argparse import Namespace

from wgsextract_cli import api
from wgsextract_cli.commands import repair


def test_repair_vcf_file_mode_writes_repaired_vcf(tmp_path):
    input_vcf = tmp_path / "ftdna.vcf"
    input_vcf.write_text(
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr1\t100\t.\tA\tG\t100\tDP=1\t.\n",
        encoding="utf-8",
    )

    api.repair.ftdna_vcf(api.RepairFtdnaVcfParams(input=input_vcf, outdir=tmp_path))

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

    result = api.repair.ftdna_vcf(api.RepairFtdnaVcfParams(input=input_vcf))

    assert "DP1" in (tmp_path / "ftdna_repaired.vcf").read_text(encoding="utf-8")
    assert result.metrics["repaired_records"] == 1


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


def test_cancelled_vcf_repair_leaves_no_partial_output(tmp_path):
    """#481 review: cancel must stop the Python record copy, not only tools."""
    input_vcf = tmp_path / "big.vcf"
    records = "".join(f"chr1\t{i}\t.\tA\tG\t100\tDP=1\t.\n" for i in range(1, 25_001))
    input_vcf.write_text(
        "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        + records,
        encoding="utf-8",
    )
    token = api.CancelToken()

    def cancel_once_started(event: api.ProgressEvent) -> None:
        if event.stage == "start":
            token.cancel()

    result = api.repair.ftdna_vcf(
        api.RepairFtdnaVcfParams(input=input_vcf),
        progress=cancel_once_started,
        cancel=token,
        check=False,
    )

    assert (result.status, result.exit_code) == ("cancelled", 130)
    assert not (tmp_path / "big_repaired.vcf").exists()


def test_repair_expands_home_in_api_paths(tmp_path, monkeypatch):
    """#481 review: a `~` outdir must not create a literal `~` folder."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ftdna.vcf").write_text(
        "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr1\t100\t.\tA\tG\t100\tDP=1\t.\n",
        encoding="utf-8",
    )

    api.repair.ftdna_vcf(
        api.RepairFtdnaVcfParams(input="~/ftdna.vcf", outdir="~/fixed")
    )

    assert "DP1" in (tmp_path / "fixed/ftdna_repaired.vcf").read_text(encoding="utf-8")
    assert not (tmp_path / "~").exists()
