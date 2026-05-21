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
        Namespace(input=str(input_vcf), output=None, _explicit_dests={"input"})
    )

    assert "DP1" in (tmp_path / "ftdna_repaired.vcf").read_text(encoding="utf-8")


def test_repair_bam_file_mode_streams_through_samtools(tmp_path, monkeypatch):
    input_bam = tmp_path / "ftdna.bam"
    input_bam.write_bytes(b"bam")
    calls = []

    def fake_run_command(
        cmd, capture_output=False, check=True, env=None, stdin=None, stdout=None
    ):
        calls.append(cmd)
        if cmd[:3] == ["samtools", "view", "-h"]:
            stdout.write(
                b"@HD\tVN:1.6\n"
                b"read 1\t99\tchr1\t100\t60\t10M\t=\t200\t100\tAAAAAAAAAA\t##########\n"
            )
            return None
        assert cmd[:4] == ["samtools", "view", "-b", "-o"]
        repaired_sam = stdin.read()
        assert "read:1" in repaired_sam
        with open(cmd[4], "wb") as output:
            output.write(b"bam")
        return None

    monkeypatch.setattr(repair, "run_command", fake_run_command)

    repair.repair_bam(
        Namespace(
            input=str(input_bam),
            outdir=str(tmp_path),
            output=None,
            _explicit_dests={"input"},
        )
    )

    assert (tmp_path / "ftdna_repaired.bam").read_bytes() == b"bam"
    assert calls[0][:3] == ["samtools", "view", "-h"]
    assert calls[1][:4] == ["samtools", "view", "-b", "-o"]


def test_repair_vcf_config_default_input_keeps_stream_mode(tmp_path, monkeypatch):
    input_vcf = tmp_path / "configured-default.vcf"
    input_vcf.write_text("##fileformat=VCFv4.2\n", encoding="utf-8")
    called = {}

    def fake_repair_vcf_stream(input_stream, output_stream):
        called["stream"] = True

    monkeypatch.setattr(
        repair, "get_script_path", lambda _name: str(tmp_path / "missing.py")
    )
    monkeypatch.setattr(repair, "repair_vcf_stream", fake_repair_vcf_stream)

    repair.repair_vcf(
        Namespace(input=str(input_vcf), output=None, _explicit_dests=set())
    )

    assert called == {"stream": True}
