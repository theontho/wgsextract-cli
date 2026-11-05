"""The wgsextract.result/1 manifest, the --json switch and exit codes (#345)."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.htslib_helpers import needs_htslib
from tests.smoke_utils import ensure_fake_data, run_cli
from tests.test_trio_vcfs import _trio
from wgsextract_cli.cli.dispatch import dispatch_command
from wgsextract_cli.cli.parser import build_parser
from wgsextract_cli.core import results
from wgsextract_cli.core.utils import (
    MissingDependencyError,
    UsageError,
    WGSExtractError,
)

CLI_ROOT = Path(__file__).resolve().parents[1]
FAKE_DIR = CLI_ROOT / "out" / "fake_30x"
SCHEMA = results.load_schema()
VALIDATOR = Draft202012Validator(SCHEMA)


def assert_valid(document: dict) -> None:
    errors = sorted(VALIDATOR.iter_errors(document), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


def dispatch(func, tmp_path: Path, *, as_json: bool = False, **extra) -> int:
    args = argparse.Namespace(
        func=func,
        command="test",
        _command="test cmd",
        outdir=str(tmp_path),
        json=as_json,
        **extra,
    )
    try:
        dispatch_command(args, set())
    except SystemExit as e:
        return int(e.code or 0)
    return 0


def test_schema_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["properties"]["schema"]["const"] == results.SCHEMA_ID


def test_fingerprint_is_cheap_copy_stable_and_content_sensitive(tmp_path):
    small = tmp_path / "small.txt"
    small.write_bytes(b"ACGT" * 10)
    large = tmp_path / "large.bin"
    large.write_bytes(os.urandom(5 << 20))
    copy = tmp_path / "copy.bin"
    shutil.copy(large, copy)
    os.utime(copy, (1, 1))

    assert results.fingerprint(large) == results.fingerprint(copy)
    assert results.fingerprint(small) != results.fingerprint(large)
    with copy.open("r+b") as handle:  # a change in a sampled block is seen
        handle.seek(0)
        handle.write(b"X")
    assert results.fingerprint(large) != results.fingerprint(copy)
    assert results.fingerprint(tmp_path / "missing") is None
    assert VALIDATOR.evolve(schema=SCHEMA["$defs"]["fingerprint"]).is_valid(
        results.fingerprint(small)
    )


@pytest.mark.parametrize(
    ("output", "manifest"),
    [
        ("out/sample_snps.vcf.gz", "out/sample_snps.vcf.manifest.json"),
        ("out/sample_mtDNA.bam", "out/sample_mtDNA.bam.manifest.json"),
        ("out/sample.tsv", "out/sample.tsv.manifest.json"),
        ("out/sample", "out/sample.manifest.json"),
    ],
)
def test_manifest_is_named_after_the_output(output, manifest):
    assert results.manifest_path_for(output) == Path(manifest)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_fifo_input_is_recorded_without_being_opened(tmp_path):
    fifo = tmp_path / "reads.fifo"
    os.mkfifo(fifo)
    written = threading.Event()

    def write_once():
        with open(fifo, "w", encoding="utf-8") as stream:  # blocks for a reader
            stream.write("payload")
        written.set()

    writer = threading.Thread(target=write_once, daemon=True)
    writer.start()
    received = []

    def command(args):
        # The writer can only finish if something already read the pipe.
        assert not written.wait(0.5), "the FIFO was drained before the command"
        with open(fifo, encoding="utf-8") as stream:
            received.append(stream.read())

    assert dispatch(command, tmp_path, input=str(fifo)) == 0
    writer.join(timeout=10)
    assert received == ["payload"]
    record = results.FileRecord.describe(fifo, "input")
    assert record.exists and record.size_bytes is None
    assert record.fingerprint is None and results.fingerprint(fifo) is None


@pytest.mark.parametrize(
    ("first", "second"),
    [("_MT.fasta", "_MT.vcf.gz"), ("_Y.bam", "_Y.vcf.gz")],
)
def test_outputs_differing_only_in_type_get_separate_manifests(tmp_path, first, second):
    def run(suffix):
        def command(args):
            out = tmp_path / f"s{suffix}"
            out.write_bytes(b"x")
            results.current_result().add_output(out, suffix)

        assert dispatch(command, tmp_path) == 0

    run(first)
    run(second)
    manifests = sorted(tmp_path.glob("*.manifest.json"))
    assert len(manifests) == 2
    roles = {json.loads(m.read_text())["outputs"][0]["role"] for m in manifests}
    assert roles == {first, second}


def test_manifest_is_written_atomically_next_to_outputs(tmp_path):
    out_vcf = tmp_path / "s_snps.vcf.gz"

    def command(args):
        out_vcf.write_bytes(b"data")
        (tmp_path / "s_snps.vcf.gz.tbi").write_bytes(b"index")
        results.current_result().add_output(out_vcf, "snps", metrics={"variants": 3})
        results.current_result().add_metrics(variants=3)
        results.warn("low_coverage", "Low coverage.", depth=2.5, threshold=10)
        print("human text")

    assert dispatch(command, tmp_path, token="hunter2") == 0
    manifest = tmp_path / "s_snps.vcf.manifest.json"
    document = json.loads(manifest.read_text())
    assert_valid(document)
    assert [p.name for p in tmp_path.iterdir() if ".tmp" in p.name] == []
    assert document["status"] == "ok" and document["manifest"] == str(manifest)
    (output,) = document["outputs"]
    assert output["role"] == "snps" and output["kind"] == "vcf"
    assert output["index"] == str(out_vcf) + ".tbi"
    assert output["metrics"] == {"variants": 3} and output["size_bytes"] == 4
    assert document["warnings"][0]["code"] == "low_coverage"
    assert document["provenance"]["params"]["token"] == "<redacted>"


@pytest.mark.parametrize(
    ("error", "code", "kind"),
    [
        (UsageError("--input required."), 2, "usage"),
        (MissingDependencyError("samtools missing"), 3, "missing_dependency"),
        (WGSExtractError("bad"), 1, "failure"),
        (RuntimeError("boom"), 1, "failure"),
    ],
)
def test_exit_codes(tmp_path, capsys, error, code, kind):
    def command(args):
        raise error

    assert dispatch(command, tmp_path, as_json=True) == code
    document = json.loads(capsys.readouterr().out)
    assert_valid(document)
    assert document["exit_code"] == code and document["error"]["kind"] == kind
    assert document["status"] == "failed"


def test_missing_tool_exits_3(tmp_path, capsys):
    from wgsextract_cli.core.dependency_checks import verify_dependencies

    def command(args):
        verify_dependencies(["wgse-no-such-tool"])

    assert dispatch(command, tmp_path, as_json=True) == 3
    assert json.loads(capsys.readouterr().out)["error"]["kind"] == (
        "missing_dependency"
    )


def test_json_stdout_is_one_document_despite_child_output(tmp_path):
    # Run in a real process so fd-level output from children is exercised.
    script = textwrap.dedent(
        f"""
        import argparse, subprocess, sys
        from wgsextract_cli.cli.dispatch import dispatch_command
        from wgsextract_cli.core.results import current_result, human_output

        def command(args):
            print("python print")
            human_output("human text")
            subprocess.run([sys.executable, "-c", "print('child stdout')"])
            current_result().add_metrics(answer=42)

        dispatch_command(argparse.Namespace(
            func=command, command="test", _command="test", json=True,
            outdir={str(tmp_path)!r}), set())
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    document = json.loads(proc.stdout)
    assert_valid(document)
    assert document["metrics"] == {"answer": 42}
    assert "python print" in proc.stderr and "child stdout" in proc.stderr
    assert "human text" not in proc.stdout + proc.stderr


def test_every_subcommand_gets_its_full_command_name():
    parser, _ = build_parser()
    args = parser.parse_args(["vcf", "snp", "--json"])
    assert args._command == "vcf snp" and args.json is True
    assert parser.parse_args(["info", "chip", "--list"])._command == "info chip"
    assert parser.parse_args(["info"])._command == "info"


def test_cli_usage_error_exits_2_with_a_manifest_on_stdout():
    rc, stdout, stderr = run_cli(["info", "--json"])
    assert rc == 2, stderr
    document = json.loads(stdout)
    assert_valid(document)
    assert document["command"] == "info" and document["error"]["kind"] == "usage"


def test_cli_extract_writes_a_valid_manifest(tmp_path):
    if not shutil.which("samtools"):
        pytest.skip("samtools not found in PATH")
    ensure_fake_data(str(FAKE_DIR))
    rc, stdout, stderr = run_cli(
        [
            "extract",
            "mt-bam",
            "--input",
            str(FAKE_DIR / "fake.bam"),
            "--ref",
            str(FAKE_DIR / "fake_ref.fa"),
            "--outdir",
            str(tmp_path),
            "--json",
        ]
    )
    assert rc == 0, stderr
    document = json.loads(stdout)
    assert_valid(document)
    assert document["command"] == "extract mt-bam"
    assert [o["role"] for o in document["outputs"]] == ["mt_bam"]
    assert document["inputs"][0]["fingerprint"].startswith("wgse-fp1:")
    assert document["provenance"]["reference"]["path"].endswith("fake_ref.fa")
    assert "samtools" in document["provenance"]["tools"]
    written = json.loads((tmp_path / "fake_mtDNA.bam.manifest.json").read_text())
    assert written == document


@pytest.mark.parametrize("pair", [("mito-fasta", "mito-vcf"), ("ydna-bam", "ydna-vcf")])
def test_cli_extract_pairs_keep_both_manifests(tmp_path, pair):
    if not (shutil.which("samtools") and shutil.which("bcftools")):
        pytest.skip("samtools/bcftools not found in PATH")
    ensure_fake_data(str(FAKE_DIR))
    common = ["--input", str(FAKE_DIR / "fake.bam")]
    common += ["--ref", str(FAKE_DIR / "fake_ref.fa"), "--outdir", str(tmp_path)]
    for command in pair:
        rc, _, stderr = run_cli(["extract", command, *common])
        assert rc == 0, stderr
    commands = set()
    for manifest in tmp_path.glob("*.manifest.json"):
        document = json.loads(manifest.read_text())
        assert_valid(document)
        commands.add(document["command"])
    assert commands == {f"extract {command}" for command in pair}


def test_urls_and_secret_options_are_redacted_from_argv_and_params(tmp_path, capsys):
    url = "https://user:pass@example.org/ref.fa.gz?token=s3cret#frag"
    argv = ["ref", "download", "--url", url, f"--mirror={url}", "--api-key", "k3y"]

    def command(args):
        results.warn("slow_mirror", f"Slow mirror {url}")
        raise WGSExtractError(f"Download failed: {url}")

    code = dispatch(
        command,
        tmp_path,
        as_json=True,
        _argv=argv,
        url=url,
        mirrors=[url],
        api_key="k3y",
    )
    assert code == 1
    stdout = capsys.readouterr().out
    for secret in ("user", "pass", "s3cret", "frag", "k3y"):
        assert secret not in stdout
    provenance = json.loads(stdout)["provenance"]
    clean = "https://example.org/ref.fa.gz"
    assert provenance["argv"] == [
        "ref",
        "download",
        "--url",
        clean,
        f"--mirror={clean}",
        "--api-key",
        "<redacted>",
    ]
    assert provenance["params"]["url"] == clean
    assert provenance["params"]["mirrors"] == [clean]
    document = json.loads(stdout)
    assert document["error"]["message"] == f"Download failed: {clean}"
    assert document["warnings"][0]["message"] == f"Slow mirror {clean}"


def test_params_report_the_genome_selection_actually_used(
    tmp_path, capsys, monkeypatch
):
    from wgsextract_cli.cli import dispatch as dispatch_module

    bam = tmp_path / "genomes" / "s.bam"

    def select(args, _explicit):
        if args.genome == "bad":
            raise UsageError("no such genome")
        args.input, args.outdir = str(bam), str(bam.parent)

    monkeypatch.setattr(dispatch_module, "apply_genome_selection", select)
    assert dispatch(lambda args: None, tmp_path, as_json=True, genome="s") == 0
    params = json.loads(capsys.readouterr().out)["provenance"]["params"]
    assert params["input"] == str(bam) and params["outdir"] == str(bam.parent)

    # A selection that fails still reports the options as given.
    assert dispatch(lambda args: None, tmp_path, as_json=True, genome="bad") == 2
    params = json.loads(capsys.readouterr().out)["provenance"]["params"]
    assert "input" not in params and params["outdir"] == str(tmp_path)


def test_manifest_never_replaces_the_run_log(tmp_path):
    log = tmp_path / "s_mtDNA.bam.manifest.json"

    def command(args):
        (tmp_path / "s_mtDNA.bam").write_bytes(b"bam")
        results.current_result().add_output(tmp_path / "s_mtDNA.bam", "mtdna")

    args = argparse.Namespace(
        func=command,
        command="test",
        _command="test",
        outdir=str(tmp_path),
        json=False,
        log_file=str(log),
    )
    from wgsextract_cli.core.run_log import run_logging

    with run_logging(args, ["test"]):
        dispatch_command(args, set())
    assert not log.read_text().lstrip().startswith("{")
    assert "is this run's log file" in log.read_text()


def test_failed_manifest_write_leaves_manifest_unset(tmp_path, capsys, monkeypatch):
    import wgsextract_cli.core.atomic_output as atomic

    def broken(*_args, **_kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(atomic, "atomic_output", broken)

    def command(args):
        (tmp_path / "s.vcf.gz").write_bytes(b"vcf")
        results.current_result().add_output(tmp_path / "s.vcf.gz", "snps")

    assert dispatch(command, tmp_path, as_json=True) == 0
    document = json.loads(capsys.readouterr().out)
    assert_valid(document)
    assert document["manifest"] is None
    assert not (tmp_path / "s.vcf.manifest.json").exists()


def test_rewrapped_dependency_error_still_exits_3(tmp_path, capsys):
    from wgsextract_cli.core.utils import ToolLaunchError

    def command(args):
        try:
            raise ToolLaunchError("Could not start haplogrep: Permission denied.")
        except OSError as e:
            raise WGSExtractError(f"Haplogrep failed: {e}") from e

    assert dispatch(command, tmp_path, as_json=True) == 3
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["kind"] == "missing_dependency"
    assert error["message"].startswith("Haplogrep failed:")


@pytest.mark.skipif(not shutil.which("bcftools"), reason="needs bcftools")
@pytest.mark.skipif(os.name == "nt", reason="needs POSIX execute permissions")
def test_cli_non_executable_haplogrep_exits_3(tmp_path):
    haplogrep = tmp_path / "haplogrep"
    haplogrep.write_text("#!/bin/sh\n")
    haplogrep.chmod(0o644)
    vcf = tmp_path / "s.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chrM,length=16569>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chrM\t73\t.\tA\tG\t50\tPASS\t.\n"
    )
    rc, stdout, stderr = run_cli(
        ["lineage", "mt-haplogroup", "--input", str(vcf)]
        + ["--haplogrep-path", str(haplogrep), "--outdir", str(tmp_path), "--json"]
    )
    assert rc == 3, stderr
    assert json.loads(stdout)["error"]["kind"] == "missing_dependency"


def test_aadr_outputs_are_matched_literally(tmp_path):
    from wgsextract_cli.commands._microarray_combined import record_aadr

    for name in ("sample[1]_AADR_1240k.geno", "sample1_AADR_1240k.geno"):
        (tmp_path / name).write_text("9\n")
    result = results.CommandResult(command="microarray")
    with results.collecting(result):
        record_aadr(str(tmp_path), "sample[1]", "1240k")
    assert [Path(r.path).name for r in result.outputs] == ["sample[1]_AADR_1240k.geno"]


def assert_outputs_match_disk(document: dict) -> None:
    assert document["outputs"], "the run registered no outputs"
    for output in document["outputs"]:
        path = Path(output["path"])
        assert path.is_file(), path
        assert output["size_bytes"] == path.stat().st_size, path
        assert output["fingerprint"] == results.fingerprint(path), path
    assert json.loads(Path(document["manifest"]).read_text()) == document


def _write_paired_reads(directory: Path, pairs: int) -> tuple[Path, Path, Path]:
    import gzip
    import random

    rng = random.Random(7)
    seq = "".join(rng.choice("ACGT") for _ in range(20000))
    ref = directory / "ref.fa"
    lines = [seq[i : i + 60] for i in range(0, len(seq), 60)]
    ref.write_text(">chr1\n" + "\n".join(lines) + "\n")
    complement = str.maketrans("ACGT", "TGCA")
    r1, r2 = directory / "reads_R1.fastq.gz", directory / "reads_R2.fastq.gz"
    with gzip.open(r1, "wt") as one, gzip.open(r2, "wt") as two:
        for i in range(pairs):
            pos = rng.randrange(0, len(seq) - 400)
            mate = seq[pos + 200 : pos + 300].translate(complement)[::-1]
            one.write(f"@r{i}/1\n{seq[pos : pos + 100]}\n+\n{'I' * 100}\n")
            two.write(f"@r{i}/2\n{mate}\n+\n{'I' * 100}\n")
    return ref, r1, r2


def test_cli_align_manifest_lists_the_alignment_and_its_read_counts(tmp_path):
    if not all(shutil.which(tool) for tool in ("bwa", "samtools")):
        pytest.skip("bwa/samtools not found in PATH")
    ref, r1, r2 = _write_paired_reads(tmp_path, pairs=150)
    rc, stdout, stderr = run_cli(
        ["align", "--r1", str(r1), "--r2", str(r2), "--ref", str(ref)]
        + ["--outdir", str(tmp_path / "out"), "--json"]
    )
    assert rc == 0, stderr
    document = json.loads(stdout)
    assert_valid(document)
    assert_outputs_match_disk(document)
    (alignment,) = [o for o in document["outputs"] if o["role"] == "alignment"]
    counted = subprocess.run(
        ["samtools", "view", "-c", alignment["path"]],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert alignment["metrics"]["records"] == int(counted) == 300
    assert document["metrics"]["aligner"] == "bwa"
    assert document["provenance"]["reference"]["path"] == str(ref)
    assert not document["warnings"]


@needs_htslib
def test_cli_trio_manifest_lists_every_mode_output(tmp_path):
    child, mother, father = _trio(
        tmp_path,
        child=["100 denovo A G 0/1:99", "300 recessive A G 1/1:99"],
        mother=["100 . A . 0/0:99", "300 . A G 0/1:99"],
        father=["100 . A . 0/0:99", "300 . A G 0/1:99"],
    )
    outdir = tmp_path / "out"
    rc, stdout, stderr = run_cli(
        ["vcf", "trio", "--proband", child, "--mother", mother, "--father", father]
        + ["--mode", "all", "--outdir", str(outdir), "--json"]
    )
    assert rc == 0, stderr
    document = json.loads(stdout)
    assert_valid(document)
    assert_outputs_match_disk(document)
    assert document["manifest"] == str(outdir / "trio.manifest.json")
    on_disk = {p.name for p in outdir.glob("trio_*.vcf.gz")}
    assert {Path(o["path"]).name for o in document["outputs"]} == on_disk
    family = {i["role"]: i for i in document["inputs"]}
    for role, path in (("proband", child), ("mother", mother), ("father", father)):
        assert family[role]["fingerprint"] == results.fingerprint(path)
    by_role = {o["role"]: o["metrics"]["variants"] for o in document["outputs"]}
    assert by_role["trio_denovo"] == document["metrics"]["denovo_variants"] == 1
    assert by_role["trio_recessive"] == document["metrics"]["recessive_variants"] == 1


def test_cli_failed_run_reports_failure_with_exit_1(tmp_path):
    if not shutil.which("tabix"):
        pytest.skip("tabix not found in PATH")
    import gzip

    broken = tmp_path / "broken.vcf.gz"
    broken.write_bytes(gzip.compress(b"not a vcf\n"))  # gzip, not BGZF
    rc, stdout, _ = run_cli(
        ["vcf", "filter", "--vcf-input", str(broken), "--expr", "QUAL>30"]
        + ["--outdir", str(tmp_path / "out"), "--json"]
    )
    document = json.loads(stdout)
    assert_valid(document)
    assert rc == document["exit_code"] == 1
    assert document["status"] == "failed" and document["error"]["kind"] == "failure"


@pytest.mark.skipif(
    sys.platform == "win32", reason="these commands are refused on Windows (exit 1)"
)
@pytest.mark.parametrize(
    "argv",
    [
        ["vcf", "sv", "--pacbio", "--ont"],
        ["vcf", "deepvariant", "--batch-size", "0"],
        ["vcf", "deepvariant", "--gpu", "--checkpoint", "model.ckpt"],
        ["vcf", "deepvariant", "--ont", "--pacbio"],
        ["realign", "--aligner", "bwa", "--preset", "map-ont"],
    ],
)
def test_cli_conflicting_options_exit_2_before_any_work(tmp_path, argv):
    ensure_fake_data(str(FAKE_DIR))
    outdir = tmp_path / "out"
    rc, stdout, _ = run_cli(
        [*argv, "--input", str(FAKE_DIR / "fake.bam")]
        + ["--ref", str(FAKE_DIR / "fake_ref.fa"), "--outdir", str(outdir), "--json"]
    )
    document = json.loads(stdout)
    assert_valid(document)
    assert rc == document["exit_code"] == 2 and document["error"]["kind"] == "usage"
    assert not list(outdir.glob("*.vcf.gz"))


@pytest.mark.skipif(
    sys.platform == "win32", reason="these commands are refused on Windows (exit 1)"
)
@pytest.mark.parametrize(
    "argv",
    [
        ["align", "--platform", "ont", "--gpu"],
        ["align", "--aligner", "bwa", "--preset", "map-ont"],
        ["align", "--aligner", "bwa", "--long-read"],
        ["align", "--aligner", "bwa-mem2", "--gpu"],
        ["align", "--aligner", "pbmm2", "--platform", "ont"],
        ["align", "--aligner", "pbmm2", "--platform", "clr", "--gpu"],
    ],
)
def test_cli_conflicting_align_options_exit_2(tmp_path, argv):
    ensure_fake_data(str(FAKE_DIR))
    reads = tmp_path / "reads.fastq"
    reads.write_text("@r1\nACGT\n+\nIIII\n")
    outdir = tmp_path / "out"
    rc, stdout, _ = run_cli(
        [*argv, "--r1", str(reads), "--ref", str(FAKE_DIR / "fake_ref.fa")]
        + ["--outdir", str(outdir), "--json"]
    )
    document = json.loads(stdout)
    assert_valid(document)
    assert rc == document["exit_code"] == 2 and document["error"]["kind"] == "usage"
    assert not list(outdir.glob("*.bam"))


@pytest.mark.parametrize(
    "argv",
    [
        ["bam", "merge", "{bam}"],
        ["bam", "merge", "{bam}", "{bam}"],
        ["bam", "to-ubam", "--r2", "{bam}"],
        ["benchmark", "--coverage", "0"],
        ["qc", "fake-data", "--legacy-bam", "--full-size"],
    ],
    ids=["merge-one", "merge-duplicate", "ubam-r2-alone", "coverage-0", "legacy"],
)
def test_cli_option_conflicts_in_other_commands_exit_2(tmp_path, argv):
    bam = tmp_path / "in.bam"
    bam.write_bytes(b"")
    outdir = tmp_path / "out"
    rc, stdout, _ = run_cli(
        [arg.format(bam=bam) for arg in argv] + ["--outdir", str(outdir), "--json"]
    )
    document = json.loads(stdout)
    assert_valid(document)
    assert rc == document["exit_code"] == 2 and document["error"]["kind"] == "usage"
    assert not document["outputs"]


def test_cli_deps_check_for_a_missing_tool_exits_3():
    rc, stdout, _ = run_cli(["deps", "check", "--tool", "wgse-no-such-tool", "--json"])
    document = json.loads(stdout)
    assert_valid(document)
    assert rc == document["exit_code"] == 3
    assert document["error"]["kind"] == "missing_dependency"


@needs_htslib
def test_cram_with_multi_reference_containers_still_reports_records(tmp_path):
    from wgsextract_cli.core.output_checks import alignment_record_counts

    contigs = {f"c{i}": "ACGTTGCAAC" * 30 for i in range(20)}
    ref = tmp_path / "ref.fa"
    ref.write_text("".join(f">{name}\n{seq}\n" for name, seq in contigs.items()))
    header = "@HD\tVN:1.6\tSO:coordinate\n" + "".join(
        f"@SQ\tSN:{name}\tLN:300\n" for name in contigs
    )
    reads = [
        f"r{name}_{start}\t0\t{name}\t{start + 1}\t60\t50M\t*\t0\t0\t"
        f"{seq[start : start + 50]}\t{'I' * 50}\n"
        for name, seq in contigs.items()
        for start in (0, 50, 100)
    ]
    sam = tmp_path / "in.sam"
    sam.write_text(header + "".join(reads))
    cram = tmp_path / "multi.cram"
    samtools = shutil.which("samtools")
    assert samtools
    subprocess.run([samtools, "faidx", str(ref)], check=True)
    # Many small contigs per slice: the containers span several references.
    subprocess.run(
        [samtools, "view", "-T", str(ref), "-O", "cram,multi_seq_per_slice=1"]
        + ["-o", str(cram), str(sam)],
        check=True,
    )
    subprocess.run([samtools, "index", str(cram)], check=True)
    assert alignment_record_counts(cram) == {"records": len(reads)}


def _write_small_bam(directory: Path, name: str, reads_per_contig: int = 3) -> Path:
    """A coordinate-sorted, indexed BAM (and its reference) from a tiny SAM."""
    samtools = shutil.which("samtools")
    assert samtools
    contigs = {f"c{i}": "ACGTTGCAAC" * 30 for i in range(3)}
    ref = directory / "ref.fa"
    if not ref.exists():
        ref.write_text("".join(f">{c}\n{seq}\n" for c, seq in contigs.items()))
        subprocess.run([samtools, "faidx", str(ref)], check=True)
    header = "@HD\tVN:1.6\tSO:coordinate\n" + "".join(
        f"@SQ\tSN:{c}\tLN:300\n" for c in contigs
    )
    header += f"@RG\tID:{name}\tSM:S1\tPL:ILLUMINA\n"
    reads = [
        f"{name}_{c}_{start}\t0\t{c}\t{start + 1}\t60\t50M\t*\t0\t0\t"
        f"{seq[start : start + 50]}\t{'I' * 50}\tRG:Z:{name}\n"
        for c, seq in contigs.items()
        for start in range(0, 50 * reads_per_contig, 50)
    ]
    sam = directory / f"{name}.sam"
    sam.write_text(header + "".join(reads))
    bam = directory / f"{name}.bam"
    subprocess.run([samtools, "view", "-b", "-o", str(bam), str(sam)], check=True)
    subprocess.run([samtools, "index", str(bam)], check=True)
    return bam


def _record_count(path: str, ref: Path) -> int:
    counted = subprocess.run(
        ["samtools", "view", "-c", "-T", str(ref), path],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return int(counted)


@needs_htslib
@pytest.mark.parametrize(
    "argv",
    [
        ["bam", "sort", "--input", "{a}"],
        ["bam", "to-cram", "--input", "{a}"],
        ["bam", "unsort", "--input", "{a}"],
        ["bam", "merge", "{a}", "{b}", "--output", "merged.bam"],
    ],
    ids=["sort", "to-cram", "unsort", "merge"],
)
def test_cli_bam_manifests_list_alignments_with_true_record_counts(tmp_path, argv):
    a = _write_small_bam(tmp_path, "a", reads_per_contig=3)
    b = _write_small_bam(tmp_path, "b", reads_per_contig=2)
    ref = tmp_path / "ref.fa"
    argv = [arg.format(a=a, b=b) for arg in argv]
    rc, stdout, stderr = run_cli(
        [*argv, "--ref", str(ref), "--outdir", str(tmp_path / "out"), "--json"]
    )
    assert rc == 0, stderr
    document = json.loads(stdout)
    assert_valid(document)
    assert_outputs_match_disk(document)
    alignments = [o for o in document["outputs"] if o["kind"] in ("bam", "cram")]
    assert len(alignments) == 1
    (alignment,) = alignments
    assert _record_count(alignment["path"], ref) == (15 if argv[1] == "merge" else 9)
    if argv[1] != "unsort":  # an unsorted BAM has no index to count from
        assert alignment["metrics"]["records"] == _record_count(alignment["path"], ref)
    if argv[1] == "merge":
        assert {i["path"] for i in document["inputs"]} >= {str(a), str(b)}
        assert document["metrics"]["merged_inputs"] == 2


@needs_htslib
def test_cli_qc_vcf_metrics_match_bcftools(tmp_path):
    ensure_fake_data(str(FAKE_DIR))
    vcf = FAKE_DIR / "fake.vcf.gz"
    rc, stdout, stderr = run_cli(
        ["qc", "vcf", "--vcf-input", str(vcf), "--outdir", str(tmp_path), "--json"]
    )
    assert rc == 0, stderr
    document = json.loads(stdout)
    assert_valid(document)
    assert_outputs_match_disk(document)
    records = subprocess.run(
        ["bcftools", "view", "-H", str(vcf)], capture_output=True, text=True, check=True
    ).stdout.count("\n")
    assert document["metrics"]["records"] == records > 0
    assert (
        document["metrics"]["snps"] + document["metrics"]["indels"]
        <= document["metrics"]["records"]
    )


def test_cli_repair_vcf_counts_only_changed_records(tmp_path):
    vcf = tmp_path / "ftdna.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "1\t100\t.\tA\tG\t50\tPASS\t.\n"
        "1\t200\t.\tC\tT\t50\tLow Qual!\t.\n"
        "1\t300\t.\tG\tA\t50\tq10\t.\n"
    )
    rc, stdout, stderr = run_cli(
        ["repair", "ftdna-vcf", "--input", str(vcf), "--outdir", str(tmp_path / "o")]
        + ["--json"]
    )
    assert rc == 0, stderr
    document = json.loads(stdout)
    assert_valid(document)
    assert_outputs_match_disk(document)
    assert document["metrics"]["records"] == 3
    assert document["metrics"]["repaired_records"] == 1


def test_cli_repair_pipe_mode_refuses_json():
    rc, stdout, _ = run_cli(["repair", "ftdna-vcf", "--json"])
    document = json.loads(stdout)
    assert_valid(document)
    assert rc == document["exit_code"] == 2 and document["error"]["kind"] == "usage"


def test_missing_optional_typing_environment_is_a_missing_dependency(monkeypatch):
    from wgsextract_cli.commands.genotyping import common

    monkeypatch.setattr(common, "pixi_env_tool", lambda tool: None)
    with pytest.raises(MissingDependencyError):
        common.env_tool("T1K", "typing")


def test_installers_record_only_the_files_they_add_or_change(tmp_path):
    library = tmp_path / "reflib"
    (library / "ref").mkdir(parents=True)
    kept = library / "ref" / "kept.vcf.gz"
    kept.write_text("old")
    (library / ".partial").mkdir()
    before = results.file_snapshot(library)
    (library / "ref" / "new.vcf.gz").write_text("new")
    (library / ".partial" / "chunk").write_text("work")
    result = results.CommandResult("ref clinvar")
    with results.collecting(result):
        changed = results.record_changed_files(library, before, "reference_resource")
    assert [Path(p).name for p in changed] == ["new.vcf.gz"]
    assert [Path(o.path).name for o in result.outputs] == ["new.vcf.gz"]


@needs_htslib
def test_cli_ref_index_lists_only_this_fastas_new_indexes(tmp_path):
    if not shutil.which("bwa"):
        pytest.skip("bwa not found in PATH")
    ref = tmp_path / "genome.fa"
    ref.write_text(">c1\n" + "ACGT" * 200 + "\n")
    other = tmp_path / "other.fa.fai"
    other.write_text("c1\t800\t4\t800\t801\n")
    rc, stdout, stderr = run_cli(["ref", "index", "--ref", str(ref), "--json"])
    assert rc == 0, stderr
    document = json.loads(stdout)
    assert_valid(document)
    assert_outputs_match_disk(document)
    names = {Path(o["path"]).name for o in document["outputs"]}
    assert {"genome.fa.fai", "genome.dict", "genome.fa.bwt"} <= names
    assert "other.fa.fai" not in names and "genome.fa" not in names
    assert not [name for name in names if name.endswith((".lock", ".guard"))]


def test_cli_sign_create_never_lists_its_own_result_manifest(tmp_path):
    data = tmp_path / "results"
    data.mkdir()
    (data / "a.txt").write_text("a")
    for _ in range(2):
        rc, stdout, stderr = run_cli(["sign", "create", str(data), "--json"])
        assert rc == 0, stderr
        document = json.loads(stdout)
        assert_valid(document)
        assert_outputs_match_disk(document)
        assert document["metrics"]["files"] == 1
    rc, stdout, stderr = run_cli(["sign", "verify", str(data / "SHA256SUMS"), "--json"])
    assert rc == 0, stderr
    document = json.loads(stdout)
    assert document["metrics"]["changed_files"] == 0
    assert [w["code"] for w in document["warnings"]] == ["unsigned"]


def test_vep_records_an_existing_output_it_skips(tmp_path):
    from wgsextract_cli.commands import _vep_run

    vcf = tmp_path / "s.vcf.gz"
    vcf.write_bytes(b"")
    _, final = _vep_run.vep_output_paths(str(vcf), str(tmp_path), "tab")
    Path(final).write_text("annotated\n")
    args = argparse.Namespace(outdir=str(tmp_path), format="tab", force=False)
    result = results.CommandResult("vep run")
    with results.collecting(result):
        status, _ = _vep_run._run_vep_on_input(args, str(vcf), "1")
    assert status == "SKIPPED"
    assert [(o.path, o.role) for o in result.outputs] == [(final, "vep_annotated")]
    assert [w.code for w in result.warnings] == ["output_exists"]


@needs_htslib
def test_analyze_carries_each_steps_manifest_outputs_and_warnings(tmp_path):
    from wgsextract_cli.commands._analyze_workflows import run_cli_subcommand

    ensure_fake_data(str(FAKE_DIR))
    step_dir = tmp_path / "qc"
    args = argparse.Namespace(
        ref=str(FAKE_DIR / "fake_ref.fa"), input=None, threads=1, debug=False
    )
    result = results.CommandResult("analyze comprehensive")
    with results.collecting(result):
        run_cli_subcommand(
            ["qc", "vcf", "--vcf-input", str(FAKE_DIR / "fake.vcf.gz")]
            + ["--outdir", str(step_dir)],
            args,
        )
    roles = {o.role: o for o in result.outputs}
    step = json.loads(Path(roles["step_manifest"].path).read_text())
    assert step["command"] == "qc vcf"
    assert {o["path"] for o in step["outputs"]} <= {o.path for o in result.outputs}
    (stats,) = (o for o in step["outputs"] if o["role"] == "vcf_stats")
    assert roles["vcf_stats"].metrics == stats["metrics"] and stats["metrics"]


@needs_htslib
def test_analyze_steps_show_their_output_and_keep_their_results(tmp_path, capfd):
    from tests.test_lineage_haplogroups import _HG38, _KIT_BODY, _haplogrep, _vcf
    from wgsextract_cli.commands._analyze_workflows import run_cli_subcommand

    bam = _write_small_bam(tmp_path, "a", reads_per_contig=3)
    kit = _vcf(tmp_path / "kit.vcf.gz", _HG38, _KIT_BODY)
    out = tmp_path / "out"
    args = argparse.Namespace(ref=None, input=None, threads=1, debug=False)
    result = results.CommandResult("analyze comprehensive")
    with results.collecting(result):
        run_cli_subcommand(
            ["info", "--detailed", "--input", str(bam), "--outdir", str(out)], args
        )
        run_cli_subcommand(
            ["lineage", "mt-haplogroup", "--input", kit, "--outdir", str(out),
             "--haplogrep-path", _haplogrep(tmp_path)],
            args,
        )  # fmt: skip
    shown = capfd.readouterr().out
    assert "By Reference Sequence Name" in shown  # info's chromosome table
    assert "Haplogroup      : H1c" in shown
    # info writes no manifest, yet its warnings, with their details, survive.
    assert not list(out.glob("info*.manifest.json"))
    (low,) = (w for w in result.warnings if w.code == "low_coverage")
    assert low.details["step"] == "info"
    assert len(low.details) > 1  # depth and threshold survive, not just the step
    roles = {o.role: o for o in result.outputs}
    assert roles["haplogroup_report"].path == str(out / "haplogrep_results.txt")
    assert not [p for p in out.iterdir() if p.name.startswith(".wgse-step-")]
