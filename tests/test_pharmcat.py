import json
from argparse import Namespace

import pytest

from wgsextract_cli.commands.vcf.pharmcat import (
    merge_outside_calls,
    pharmcat_directory,
    summarize,
)
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import WGSExtractError


def test_a_missing_pharmcat_install_says_where_to_get_it(tmp_path):
    lib = ReferenceLibrary(str(tmp_path))
    with pytest.raises(WGSExtractError) as error:
        pharmcat_directory(Namespace(pharmcat_dir=None), lib)
    message = str(error.value)
    assert str(tmp_path / "pharmcat") in message
    assert "github.com/PharmGKB/PharmCAT/releases" in message
    assert "pharmcat_directory" in message and "--pharmcat-dir" in message


def test_an_extracted_release_in_the_library_is_found(tmp_path):
    release = tmp_path / "pharmcat"
    release.mkdir()
    for name in ("pharmcat_pipeline", "pharmcat.jar", "pharmcat_regions.bed"):
        (release / name).write_text("x")
    lib = ReferenceLibrary(str(tmp_path))
    assert pharmcat_directory(Namespace(pharmcat_dir=None), lib) == str(release)


def test_summary_lists_called_genes_and_skips_unknown(tmp_path):
    phenotype = tmp_path / "s.phenotype.json"
    phenotype.write_text(
        json.dumps(
            {
                "geneReports": {
                    "CYP2C19": {
                        "recommendationDiplotypes": [
                            {
                                "allele1": {"name": "*17"},
                                "allele2": {"name": "*17"},
                                "phenotypes": ["Ultrarapid Metabolizer"],
                            }
                        ]
                    },
                    "CYP2D6": {
                        "recommendationDiplotypes": [
                            {
                                "allele1": {"name": "Unknown"},
                                "allele2": {"name": "Unknown"},
                            }
                        ]
                    },
                }
            }
        )
    )
    assert summarize(str(phenotype)) == [
        ("CYP2C19", "*17/*17", "Ultrarapid Metabolizer")
    ]


def test_outside_calls_merge_and_reject_a_gene_called_twice(tmp_path):
    cyp2d6 = tmp_path / "s.cyp2d6.pharmcat.tsv"
    cyp2d6.write_text("CYP2D6\t*1/*2\u22653\n", encoding="utf-8")
    hla = tmp_path / "s.hla.pharmcat.tsv"
    hla.write_text("# T1K\nHLA-A\t*02:01/*24:02\nHLA-B\t*07:02/*57:01", "utf-8")
    merged = tmp_path / "merged.tsv"

    merge_outside_calls([str(cyp2d6), str(hla)], str(merged))
    assert merged.read_text(encoding="utf-8") == (
        "CYP2D6\t*1/*2\u22653\nHLA-A\t*02:01/*24:02\nHLA-B\t*07:02/*57:01\n"
    )
    with pytest.raises(WGSExtractError, match="CYP2D6 is called in both"):
        merge_outside_calls([str(cyp2d6), str(cyp2d6)], str(merged))
