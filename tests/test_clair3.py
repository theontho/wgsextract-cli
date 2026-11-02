import argparse
import hashlib
import platform
import sys
from pathlib import Path

import pytest

from wgsextract_cli.cli.parser import build_parser
from wgsextract_cli.cli.platform_support import verify_platform_support
from wgsextract_cli.commands.vcf import clair3
from wgsextract_cli.core import dependency_checks, pixi_tools
from wgsextract_cli.core.utils import WGSExtractError

DORADO_V500 = "@RG\tID:run_dna_r10.4.1_e8.2_400bps_sup@v5.0.0\tDS:basecall_model=dna_r10.4.1_e8.2_400bps_sup@v5.0.0\n"


def make_model(root: Path, name: str, content: bytes = b"weights") -> Path:
    model = root / name
    model.mkdir(parents=True)
    for file in clair3.MODEL_FILES:
        (model / file).write_bytes(content)
    return model


@pytest.fixture
def no_bundled(monkeypatch):
    monkeypatch.setattr(clair3, "bundled_models_dir", lambda: None)
    monkeypatch.delitem(clair3.settings, "clair3_model", raising=False)


@pytest.mark.parametrize(
    ("header", "model"),
    [
        (DORADO_V500, "r1041_e82_400bps_sup_v500"),
        (
            "@PG\tID:basecaller\tPN:dorado\tCL:dorado basecaller "
            "dna_r10.4.1_e8.2_400bps_hac@v4.1.0 pod5/\n",
            "r1041_e82_400bps_hac_v410",
        ),
        ("@RG\tID:x\tDS:basecall_model=dna_r9.4.1_450bps_hac\n", "r941_prom_sup_g5014"),
        (
            "@RG\tID:x\tDS:basecall_model=dna_r9.4.1_e8_fast@v3.4\n",
            "r941_prom_sup_g5014",
        ),
        (
            "@RG\tID:x\tDS:basecall_model=dna_r10.4_e8.1_sup@v3.4\n",
            "r104_e81_sup_g5015",
        ),
    ],
)
def test_basecaller_metadata_selects_model(header, model):
    (chemistry,) = clair3.basecaller_chemistries(header)
    assert clair3.model_for_chemistry(chemistry) == model


def test_only_read_group_and_program_lines_are_metadata():
    header = "@CO\tdna_r9.4.1_450bps_hac\n@RG\tID:x\tSM:HG002\n"
    assert clair3.basecaller_chemistries(header) == set()


def test_model_precedence_cli_then_config_then_metadata(tmp_path, monkeypatch):
    for name in ("cli", "config", "r1041_e82_400bps_sup_v500"):
        make_model(tmp_path, name)
    monkeypatch.setattr(clair3, "bundled_models_dir", lambda: tmp_path)
    monkeypatch.setattr(clair3, "verify_model", lambda model: None)
    monkeypatch.setitem(clair3.settings, "clair3_model", str(tmp_path / "config"))

    pick = clair3.select_model
    args = argparse.Namespace(model=str(tmp_path / "cli"))
    assert pick(args, DORADO_V500) == tmp_path / "cli"
    assert pick(argparse.Namespace(model=None), DORADO_V500) == tmp_path / "config"
    monkeypatch.delitem(clair3.settings, "clair3_model")
    assert pick(argparse.Namespace(model=None), DORADO_V500) == (
        tmp_path / "r1041_e82_400bps_sup_v500"
    )


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        ("@HD\tVN:1.6\n", "doesn't name a known basecaller"),
        (
            DORADO_V500 + "@RG\tID:b\tDS:basecall_model=dna_r9.4.1_450bps_hac\n",
            "several basecaller models",
        ),
    ],
)
def test_unknown_or_mixed_basecaller_requires_model(no_bundled, header, reason):
    with pytest.raises(WGSExtractError, match=f"{reason}.*--model"):
        clair3.select_model(argparse.Namespace(model=None), header)


def test_refusal_lists_only_bundled_ont_models(tmp_path, monkeypatch):
    for name in ("hifi_revio", "r1041_e82_400bps_sup_v520_with_mv", "ont"):
        make_model(tmp_path, name)
    monkeypatch.setattr(clair3, "bundled_models_dir", lambda: tmp_path)
    monkeypatch.delitem(clair3.settings, "clair3_model", raising=False)
    with pytest.raises(WGSExtractError, match=r"Bundled ONT models: ont\.$"):
        clair3.select_model(argparse.Namespace(model=None), "")


def test_missing_pinned_model_names_its_download(no_bundled):
    with pytest.raises(
        WGSExtractError, match="clair3_models_rerio_pytorch/r1041_e82_400bps_sup_v420"
    ):
        clair3.select_model(
            argparse.Namespace(model="r1041_e82_400bps_sup_v420"), "@HD\tVN:1.6\n"
        )


@pytest.mark.parametrize(
    "model",
    [
        "r941_prom_sup_g5014",
        "r104_e81_sup_g5015",
        "r1041_e82_260bps_sup_v400",
        "hifi_revio",
        "ilmn",
        "r1041_e82_400bps_sup_v520_with_mv",
    ],
)
def test_wrong_chemistry_or_platform_model_is_refused(model):
    with pytest.raises(WGSExtractError, match="Clair3 model"):
        clair3.check_model_matches(model, clair3.basecaller_chemistries(DORADO_V500))


def test_other_basecaller_version_only_warns(caplog):
    clair3.check_model_matches(
        "r1041_e82_400bps_hac_v410", clair3.basecaller_chemistries(DORADO_V500)
    )
    assert "different basecaller accuracy" in caplog.text
    assert "r1041_e82_400bps_sup_v500" in caplog.text


def test_r9_model_accepts_any_r9_reads(caplog):
    reads = clair3.basecaller_chemistries(
        "@RG\tID:x\tDS:basecall_model=dna_r9.4.1_450bps_hac\n"
    )
    clair3.check_model_matches("r941_prom_hac_g360+g422", reads)
    clair3.check_model_matches("ont_guppy5", reads)
    assert not caplog.text


def test_model_checksums_are_verified(tmp_path, monkeypatch, caplog):
    good = hashlib.sha256(b"weights").hexdigest()
    pins = {
        "pinned": {
            "url": "https://example.org/pinned/",
            "files": dict.fromkeys(clair3.MODEL_FILES, good),
        }
    }
    monkeypatch.setattr(clair3, "pinned_models", lambda: pins)

    clair3.verify_model(make_model(tmp_path / "ok", "pinned"))
    with pytest.raises(WGSExtractError, match="pinned SHA-256.*example.org/pinned"):
        clair3.verify_model(make_model(tmp_path / "bad", "pinned", b"corrupt"))
    clair3.verify_model(make_model(tmp_path, "custom"))
    assert "custom has no pinned checksum" in caplog.text


def test_pinned_manifest_covers_bundled_ont_defaults():
    pins = clair3.pinned_models()
    for name in (
        "r941_prom_sup_g5014",
        "r1041_e82_400bps_sup_v500",
        "r104_e81_sup_g5015",
    ):
        assert set(pins[name]["files"]) == set(clair3.MODEL_FILES)


def test_v1_tensorflow_model_directory_is_refused(tmp_path, no_bundled):
    model = tmp_path / "r941_prom_sup_g5014"
    model.mkdir()
    (model / "pileup.index").write_text("")
    with pytest.raises(WGSExtractError, match="PyTorch models"):
        clair3.resolve_model(str(model), None)


def test_command_construction(tmp_path, monkeypatch):
    ref = tmp_path / "ref.fa"
    ref.write_text(">chr20\nACGT\n")
    (tmp_path / "ref.fa.fai").write_text("chr20\t2000000\t7\t4\t5\n")
    model = make_model(tmp_path, "r1041_e82_400bps_sup_v500")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[0] == clair3.CLAIR3:
            output = Path(cmd[-2].split("=", 1)[1])
            output.mkdir(parents=True)
            (output / "merge_output.vcf.gz").write_bytes(b"vcf")
            bed = Path(cmd[-1].split("=", 1)[1])
            calls.append(bed.read_text())
        else:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"vcf")

    monkeypatch.setattr(clair3, "require_supported_platform", lambda: None)
    monkeypatch.setattr(clair3, "verify_dependencies", lambda tools: None)
    monkeypatch.setattr(clair3, "log_dependency_info", lambda tools: None)
    monkeypatch.setattr(
        clair3, "get_base_args", lambda args: ("2", str(tmp_path), str(ref), None)
    )
    monkeypatch.setattr(
        clair3, "get_bam_header", lambda *_: DORADO_V500.replace("\n", "\tSM:HG002\n")
    )
    monkeypatch.setattr(clair3, "select_model", lambda args, header: model)
    monkeypatch.setattr(clair3, "run_command", fake_run)
    monkeypatch.setattr(clair3, "ensure_vcf_indexed", lambda vcf: None)
    monkeypatch.setattr(clair3, "warn_if_no_variants", lambda *args: None)
    monkeypatch.setenv("PYTHONPATH", "/ours")

    args = build_parser()[0].parse_args(
        ["vcf", "clair3", "--input", "reads.bam", "--region", "chr20:1001-2000"]
    )
    args.input_platform = "ont"
    clair3.cmd_clair3(args)

    (command, kwargs), bed, (publish, _) = calls
    assert command[:7] == [
        "run_clair3.sh",
        "--bam_fn=reads.bam",
        f"--ref_fn={ref}",
        "--threads=2",
        "--platform=ont",
        f"--model_path={model}",
        "--sample_name=HG002",
    ]
    output = Path(command[7].removeprefix("--output="))
    assert output.parent.parent == tmp_path
    assert output.parent.name.startswith(".wgse_clair3_")
    assert command[8].endswith("regions.bed")
    assert bed == "chr20\t1000\t2000\n"
    assert kwargs["stdin"] == clair3.subprocess.DEVNULL
    assert "PYTHONPATH" not in kwargs["env"]
    assert publish[:3] == ["bcftools", "view", "-Oz"]
    assert (tmp_path / "clair3.vcf.gz").is_file()
    assert not list(tmp_path.glob(".wgse_clair3_*"))


def test_non_ont_input_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(clair3, "require_supported_platform", lambda: None)
    monkeypatch.setattr(clair3, "verify_dependencies", lambda tools: None)
    monkeypatch.setattr(clair3, "log_dependency_info", lambda tools: None)
    monkeypatch.setattr(
        clair3, "get_base_args", lambda args: ("1", str(tmp_path), "r.fa", None)
    )
    args = argparse.Namespace(input="reads.bam", input_platform="pacbio", model=None)
    with pytest.raises(WGSExtractError, match="looks like pacbio"):
        clair3.cmd_clair3(args)


@pytest.mark.parametrize(
    ("header", "sample"),
    [
        ("@RG\tID:a\tSM:HG002\n@RG\tID:b\tSM:HG002\n", "HG002"),
        ("@RG\tID:a\n", "reads"),
    ],
)
def test_sample_name_comes_from_read_groups(header, sample):
    assert clair3.sample_name(header, "/data/reads.sorted.bam") == sample


def test_several_samples_are_refused():
    with pytest.raises(WGSExtractError, match="several samples"):
        clair3.sample_name("@RG\tID:a\tSM:A\n@RG\tID:b\tSM:B\n", "reads.bam")


def test_mixed_platform_read_groups_are_refused():
    clair3.require_ont_read_groups("@RG\tID:a\tPL:ONT\n@RG\tID:b\n")
    with pytest.raises(WGSExtractError, match="ILLUMINA, ONT"):
        clair3.require_ont_read_groups("@RG\tID:a\tPL:ONT\n@RG\tID:b\tPL:ILLUMINA\n")


def test_missing_clair3_names_its_pixi_environment(monkeypatch):
    monkeypatch.setattr(clair3, "require_supported_platform", lambda: None)
    monkeypatch.setattr(dependency_checks, "require_native_tool", lambda tool: None)
    # The clair3 environment is locked for linux-64 only; CI also runs on ARM.
    monkeypatch.setattr(pixi_tools, "_locked_package_available", lambda *args: True)
    monkeypatch.setattr(
        dependency_checks, "check_dependencies", lambda tools: [clair3.CLAIR3]
    )
    with pytest.raises(WGSExtractError, match=r"run_clair3\.sh.*-e clair3"):
        clair3.cmd_clair3(argparse.Namespace(input="reads.bam"))


def test_windows_is_refused_before_inputs_are_resolved(monkeypatch):
    args = build_parser()[0].parse_args(["vcf", "clair3", "--input", "reads.bam"])
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(WGSExtractError, match="not available on Windows.*WSL2"):
        verify_platform_support(args)


@pytest.mark.parametrize(
    ("host", "machine"),
    [("linux", "aarch64"), ("darwin", "arm64"), ("darwin", "x86_64")],
)
def test_only_linux_x86_64_is_qualified(monkeypatch, host, machine):
    monkeypatch.setattr(sys, "platform", host)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    with pytest.raises(WGSExtractError, match="Linux x86-64 only"):
        clair3.require_supported_platform()
