import argparse

import pytest

from tests import test_microarray_indels as indel_fixtures
from tests.htslib_helpers import needs_tools, run_wgsextract
from tests.test_microarray_vendor_formats import _kit, _templates
from wgsextract_cli.commands import microarray
from wgsextract_cli.core import microarray_identity as identity
from wgsextract_cli.core.microarray_indels import indel_manifest_bindings
from wgsextract_cli.core.microarray_utils import convert_to_vendor_format
from wgsextract_cli.core.utils import WGSExtractError, run_command

alignment = indel_fixtures.alignment
probe_inputs = indel_fixtures.probe_inputs


@pytest.mark.parametrize(
    ("formats", "entries", "message"),
    [
        ("23andme_v5_2025", ["bad"], "FORMAT=PATH"),
        ("all", ["23andme_v5_2025={path}"], "selected"),
        ("ftdna_v2", ["ftdna_v2={path}"], "No verified"),
        ("unknown", ["unknown={path}"], "No verified"),
        (
            "23andme_v5_2025",
            ["23andme_v5_2025={path}", "23andMe-V5-2025={path}"],
            "Duplicate",
        ),
        ("23andme_v5_2025", ["23andme_v5_2025={path}.missing"], "not found"),
    ],
)
def test_manifest_bindings_reject_unsupported_or_ambiguous_requests(
    tmp_path, formats, entries, message
):
    path = tmp_path / "manifest.csv"
    path.touch()
    args = argparse.Namespace(
        formats=formats, indel_manifest=[entry.format(path=path) for entry in entries]
    )
    with pytest.raises(WGSExtractError, match=message):
        indel_manifest_bindings(args, microarray._resolve_microarray_format)


@pytest.mark.parametrize(
    ("fmt", "suffix", "body", "expected"),
    [
        (
            "23andMe_V5_2025",
            ".txt",
            "rs1\t1\t700\nrs2\t1\t701\nrs20\t1\t702\nrs3\t1\t703\n",
            "rs1\t1\t700\tDI\nrs2\t1\t701\t--\nrs20\t1\t702\t--\nrs3\t1\t703\tGG\n",
        ),
        (
            "Ancestry_V2_2024",
            ".txt",
            "rs1\t1\t700\nrs2\t1\t701\nrs20\t1\t702\nrs3\t1\t703\n",
            "rs1\t1\t700\tD\tI\nrs2\t1\t701\t0\t0\nrs20\t1\t702\t0\t0\nrs3\t1\t703\tG\tG\n",
        ),
        (
            "MyHeritage_GSA_2025",
            ".csv",
            '"rs1","1","700",\n"rs2","1","701",\n"rs20","1","702",\n"rs3","1","703",\n',
            '"rs1","1","700","DI"\n"rs2","1","701","--"\n"rs20","1","702","--"\n"rs3","1","703","GG"\n',
        ),
    ],
)
def test_indel_overlay_uses_real_vendor_layouts_without_restoring_retired_ids(
    tmp_path, monkeypatch, fmt, suffix, body, expected
):
    monkeypatch.setattr(
        identity,
        "load_rsid_map",
        lambda: {"rs20": identity.RsidIdentity(None, "withdrawn")},
    )
    root = _templates(tmp_path / "lib", fmt + suffix, "", body)
    kit = _kit(
        tmp_path / "kit.txt",
        [
            ("rs1", "1", "700", "CC"),
            ("rs2", "1", "701", "AA"),
            ("rs20", "1", "702", "AA"),
            ("rs3", "1", "703", "GG"),
        ],
    )
    out = tmp_path / ("export" + suffix)
    convert_to_vendor_format(
        fmt,
        kit,
        str(out),
        root,
        identity=identity.MicroarrayIdentity(resolve_rsids=True),
        indel_calls={
            ("rs1", "1", "700"): "DI",
            ("rs2", "1", "701"): "--",
            ("rs20", "1", "702"): "DD",
        },
    )
    assert out.read_text() == expected


@needs_tools("bcftools", "samtools", "bgzip", "tabix")
@pytest.mark.parametrize(
    ("options", "expected"),
    [([], "DI"), (["--min-dp", "30"], "--"), (["--resolve-rsids"], "--")],
)
def test_cli_indel_opt_in_preserves_combinedkit_and_honors_masks(
    tmp_path, monkeypatch, alignment, probe_inputs, options, expected
):
    manifest, lib = probe_inputs
    monkeypatch.setattr(microarray, "ReferenceLibrary", lambda *a, **k: lib)
    monkeypatch.setattr(
        microarray, "_template_search_roots", lambda *a: [str(tmp_path)]
    )
    monkeypatch.setattr(
        identity,
        "load_rsid_map",
        lambda: {"rsProbe": identity.RsidIdentity(None, "withdrawn")},
    )
    target = tmp_path / "targets.tsv.gz"
    plain = tmp_path / "targets.tsv"
    plain.write_text("chr1\t700\tC\n")
    with target.open("wb") as stream:
        run_command(["bgzip", "-c", str(plain)], stdout=stream)
    run_command(["tabix", "-s", "1", "-b", "2", "-e", "2", str(target)])
    common = [
        "microarray",
        "--input",
        alignment["bam"],
        "--ref",
        lib.fasta,
        "--ref-vcf-tab",
        str(target),
        "--threads",
        "1",
        "--region",
        "chr1:650-780",
        "--formats",
        "23andme_v5_2025",
        *options,
    ]
    before, after = tmp_path / "before", tmp_path / "after"
    run_wgsextract(*common, "--outdir", str(before))
    run_wgsextract(
        *common,
        "--outdir",
        str(after),
        "--indel-manifest",
        "23andme_v5_2025=" + manifest,
    )
    combined = next(before.glob("*CombinedKit.txt"))
    assert (after / combined.name).read_bytes() == combined.read_bytes()
    assert not list(before.glob("Indels_*"))
    exported = next(after.glob("*23andMe_V5_2025.txt")).read_text()
    assert exported.rstrip().endswith("rsProbe\t1\t700\t" + expected)
    assert list(after.glob("Indels_*/summary.json"))
