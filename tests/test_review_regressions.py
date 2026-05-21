import pytest

from wgsextract_cli.core.utils import WGSExtractError


def test_tabix_annotation_preparation_indexes_tsv_with_coordinate_columns(
    monkeypatch, tmp_path
):
    from wgsextract_cli.commands import _vcf_annotation_helpers as helpers

    resource = tmp_path / "alphamissense_hg38.tsv.gz"
    resource.touch()
    commands = []

    monkeypatch.setattr(helpers, "get_tool_path", lambda tool: tool)
    monkeypatch.setattr(
        helpers, "run_command", lambda cmd, **_kwargs: commands.append(cmd)
    )

    assert helpers.prepare_tabix_annotation(str(resource), "AlphaMissense") == str(
        resource
    )
    assert commands == [["tabix", "-f", "-s", "1", "-b", "2", "-e", "2", str(resource)]]


def test_tabix_annotation_preparation_rebuilds_stale_index(monkeypatch, tmp_path):
    from wgsextract_cli.commands import _vcf_annotation_helpers as helpers

    resource = tmp_path / "phylop_hg38.tsv.gz"
    index = tmp_path / "phylop_hg38.tsv.gz.tbi"
    resource.touch()
    index.touch()
    commands = []

    monkeypatch.setattr(helpers, "get_tool_path", lambda tool: tool)
    monkeypatch.setattr(
        helpers, "run_command", lambda cmd, **_kwargs: commands.append(cmd)
    )
    monkeypatch.setattr(
        helpers.os.path,
        "getmtime",
        lambda path: 20 if str(path).endswith(".tsv.gz") else 10,
    )

    assert helpers.prepare_tabix_annotation(str(resource), "PhyloP") == str(resource)
    assert commands == [["tabix", "-f", "-s", "1", "-b", "2", "-e", "2", str(resource)]]


def test_build_choices_include_lowercase_aliases():
    from wgsextract_cli.core.builds import (
        BUILD_CHOICES,
        HG37_BUILD_ALIASES,
        HG38_BUILD_ALIASES,
        T2T_BUILD_ALIASES,
    )

    choices = set(BUILD_CHOICES)
    assert HG37_BUILD_ALIASES <= choices
    assert HG38_BUILD_ALIASES <= choices
    assert T2T_BUILD_ALIASES <= choices


def test_annotation_resolver_accepts_mixed_case_build_suffix(tmp_path):
    from wgsextract_cli.core.reference_resolver import ReferenceLibrary

    resource = tmp_path / "revel_GRCh38.tsv.gz"
    resource.touch()
    lib = ReferenceLibrary.__new__(ReferenceLibrary)
    lib.build = "hg38"

    resolved = lib._resolve_annotation_file(None, "revel", [".tsv.gz"], [str(tmp_path)])
    assert resolved is not None
    assert resolved.lower() == str(resource).lower()


def test_alignment_pipeline_reports_process_failure():
    from wgsextract_cli.commands.align import _check_pipeline_process

    class FailingProcess:
        returncode = 1

        def wait(self):
            return self.returncode

    with pytest.raises(WGSExtractError, match="aligner failed"):
        _check_pipeline_process(FailingProcess(), "aligner")


def test_gatk_prepares_regional_cram_as_temporary_bam(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from wgsextract_cli.commands import _vcf_callers

    cram = tmp_path / "sample.cram"
    cram.touch()
    commands = []

    monkeypatch.setattr(_vcf_callers, "get_tool_path", lambda tool: tool)
    monkeypatch.setattr(
        _vcf_callers, "run_command", lambda cmd, **_kwargs: commands.append(cmd)
    )

    args = SimpleNamespace(input=str(cram), region="chrM")
    gatk_input, temp_input = _vcf_callers._prepare_gatk_input(
        args, str(tmp_path / "ref.fa"), str(tmp_path)
    )

    assert gatk_input == temp_input
    assert gatk_input.endswith(".bam")
    assert commands == [
        [
            "samtools",
            "view",
            "-bh",
            "-T",
            str(tmp_path / "ref.fa"),
            "-o",
            gatk_input,
            str(cram),
            "chrM",
        ],
        ["samtools", "index", gatk_input],
    ]


def test_gatk_does_not_convert_whole_cram_without_region(tmp_path):
    from types import SimpleNamespace

    from wgsextract_cli.commands import _vcf_callers

    args = SimpleNamespace(input=str(tmp_path / "sample.cram"), region=None)

    assert _vcf_callers._prepare_gatk_input(args, "ref.fa", str(tmp_path)) == (
        str(tmp_path / "sample.cram"),
        None,
    )


def test_structural_reference_header_keeps_reference_sq_order():
    from io import StringIO

    from wgsextract_cli.commands import _vcf_structural

    sink = StringIO()

    _vcf_structural._write_reference_header(
        sink,
        [
            "@HD\tVN:1.6\tSO:coordinate\n",
            "@RG\tID:sample\n",
            "@PG\tID:samtools\n",
        ],
        ["@SQ\tSN:chr1\tLN:248956422\n", "@SQ\tSN:chrM\tLN:16569\n"],
    )

    assert sink.getvalue().splitlines() == [
        "@HD\tVN:1.6\tSO:coordinate",
        "@SQ\tSN:chr1\tLN:248956422",
        "@SQ\tSN:chrM\tLN:16569",
        "@RG\tID:sample",
        "@PG\tID:samtools",
    ]


def test_delly_map_validation_rejects_bad_gzip(tmp_path):
    from wgsextract_cli.commands import _vcf_structural

    bad_map = tmp_path / "hg38.map.gz"
    bad_map.write_text("404: Not Found", encoding="utf-8")

    with pytest.raises(WGSExtractError, match="not a valid gzip-compressed file"):
        _vcf_structural._validate_delly_map(str(bad_map))
