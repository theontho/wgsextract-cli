import hashlib
import os
import platform
import shutil
import sys
from pathlib import Path

import pytest

from tests.fake_tools import FakeClair3, require_bare_name_fakes
from tests.htslib_helpers import (
    needs_tools,
    require_runnable,
    run_wgsextract,
    snp_sample,
    vcf_calls,
)
from wgsextract_cli.cli.parser import build_parser
from wgsextract_cli.cli.platform_support import verify_platform_support
from wgsextract_cli.commands.vcf import clair3
from wgsextract_cli.core.dependencies import clear_tool_caches
from wgsextract_cli.core.utils import (
    MissingDependencyError,
    WGSExtractError,
    run_command,
)

# Found before fake_pixi replaces PATH.
CORE_TOOLS = {tool: shutil.which(tool) for tool in ("samtools", "bcftools", "tabix")}
DORADO_V500 = "@RG\tID:run_dna_r10.4.1_e8.2_400bps_sup@v5.0.0\tDS:basecall_model=dna_r10.4.1_e8.2_400bps_sup@v5.0.0\n"


def make_model(root: Path, name: str, content: bytes = b"weights") -> Path:
    model = root / name
    model.mkdir(parents=True)
    for file in clair3.MODEL_FILES:
        (model / file).write_bytes(content)
    return model


@pytest.fixture
def linux_x86(monkeypatch):
    """Clair3 is qualified on Linux x86-64 only."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")


@pytest.fixture
def fake_clair3(tmp_path, monkeypatch) -> FakeClair3:
    """Clair3 on PATH with an empty bundled models folder, and no clair3_model
    in config.toml."""
    require_bare_name_fakes()
    bindir = tmp_path / "clair3" / "bin"
    fake = FakeClair3(bindir, shutil.which("bcftools") or "bcftools")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delitem(clair3.settings, "clair3_model", raising=False)
    clear_tool_caches()
    return fake


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


def test_model_precedence_cli_then_config_then_metadata(
    tmp_path, monkeypatch, fake_clair3
):
    for name in ("cli", "config"):
        make_model(tmp_path, name)
    make_model(fake_clair3.models, "r1041_e82_400bps_sup_v500", b"corrupt")

    pick = clair3.select_model
    monkeypatch.setitem(clair3.settings, "clair3_model", str(tmp_path / "config"))
    assert pick(str(tmp_path / "cli"), DORADO_V500) == tmp_path / "cli"
    assert pick(None, DORADO_V500) == tmp_path / "config"
    monkeypatch.delitem(clair3.settings, "clair3_model")
    # The metadata picks the bundled model, whose weights are then checked.
    with pytest.raises(
        WGSExtractError, match=r"models.r1041_e82_400bps_sup_v500.\w+\.pt doesn't match"
    ):
        pick(None, DORADO_V500)


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
def test_unknown_or_mixed_basecaller_requires_model(fake_clair3, header, reason):
    with pytest.raises(WGSExtractError, match=f"{reason}.*--model"):
        clair3.select_model(None, header)


def test_refusal_lists_only_bundled_ont_models(fake_clair3):
    for name in ("hifi_revio", "r1041_e82_400bps_sup_v520_with_mv", "ont"):
        make_model(fake_clair3.models, name)
    with pytest.raises(WGSExtractError, match=r"Bundled ONT models: ont\.$"):
        clair3.select_model(None, "")


def test_missing_pinned_model_names_its_download(fake_clair3):
    with pytest.raises(
        WGSExtractError, match="clair3_models_rerio_pytorch/r1041_e82_400bps_sup_v420"
    ):
        clair3.select_model("r1041_e82_400bps_sup_v420", "@HD\tVN:1.6\n")


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


def test_v1_tensorflow_model_directory_is_refused(tmp_path):
    model = tmp_path / "r941_prom_sup_g5014"
    model.mkdir()
    (model / "pileup.index").write_text("")
    with pytest.raises(WGSExtractError, match="PyTorch models"):
        clair3.resolve_model(str(model), None)


DORADO_RG = (
    "@RG\tID:rg\tSM:HG002\tPL:ONT\tDS:basecall_model=dna_r10.4.1_e8.2_400bps_sup@v5.0.0"
)


@needs_tools("samtools", "bcftools", "tabix")
def test_clair3_calls_the_region_for_the_reads_sample(
    tmp_path, monkeypatch, linux_x86, fake_clair3, open_stdin
):
    """Clair3 gets the region as a BED, the sample name, a closed stdin and no
    PYTHONPATH of ours; its merged VCF is published and its work folder removed."""
    require_runnable("samtools", "bcftools", "tabix")
    reads = snp_sample(tmp_path, DORADO_RG)
    model = make_model(tmp_path, "my_r1041_model")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "our-site-packages"))
    outdir = tmp_path / "out"
    run_wgsextract(
        "vcf", "clair3", "--input", reads.bam, "--ref", reads.ref,
        "--outdir", str(outdir), "--model", str(model), "-r", "chr1:900-1100",
    )  # fmt: skip

    vcf = outdir / "clair3.vcf.gz"
    assert vcf_calls(vcf) == reads.calls[:1]
    samples = run_command(["bcftools", "query", "-l", str(vcf)], capture_output=True)
    assert samples.stdout.split() == ["HG002"]
    assert sorted(p.name for p in outdir.iterdir()) == [
        "clair3.vcf.gz",
        "clair3.vcf.gz.tbi",
    ]
    (run,) = fake_clair3.runs()
    assert run["bed"] == "chr1\t899\t1100\n"
    # contract: Nanopore reads called with the chosen model.
    assert (run["opts"]["platform"], run["opts"]["model_path"]) == ("ont", str(model))


@needs_tools("samtools")
def test_non_ont_input_is_refused(tmp_path, linux_x86, fake_clair3):
    require_runnable("samtools", "bcftools", "tabix")
    reads = snp_sample(tmp_path, "@RG\tID:rg\tSM:HG002\tPL:PACBIO")
    with pytest.raises(WGSExtractError, match="looks like pacbio"):
        run_wgsextract("vcf", "clair3", "--input", reads.bam, "--ref", reads.ref)
    assert fake_clair3.runs() == []


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


@pytest.mark.skipif(
    sys.platform == "win32", reason="a batch file can't take a multi-line argument"
)
def test_missing_clair3_names_its_pixi_environment(fake_pixi, monkeypatch, linux_x86):
    # The host has the core tools, and the lockfile has clair3 for this platform.
    for tool, found in CORE_TOOLS.items():
        if not found:
            pytest.skip(f"{tool} is not on PATH")
        (fake_pixi.host_bin / tool).symlink_to(found)
    monkeypatch.setattr(sys, "prefix", str(fake_pixi.env("default")))
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "default")
    fake_pixi.env("clair3", locked=("clair3",), installed=False)
    clear_tool_caches()

    with pytest.raises(MissingDependencyError, match=r"run_clair3\.sh.*-e clair3"):
        run_wgsextract("vcf", "clair3", "--input", "reads.bam")


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
