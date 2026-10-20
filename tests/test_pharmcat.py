import json
from argparse import Namespace

import pytest

from wgsextract_cli.commands.vcf.pharmcat import pharmcat_directory, summarize
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
