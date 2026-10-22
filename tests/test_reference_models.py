import csv
from importlib import resources

import pytest

from wgsextract_cli.core.builds import build_from_path, canonical_build
from wgsextract_cli.core.constants import REFERENCE_MODELS
from wgsextract_cli.core.reference_models import identify_reference
from wgsextract_cli.core.variant_files import calculate_bam_md5

HG19 = {"1": 249250621, "X": 155270560, "Y": 59373566, "MT": 16569}
HG38 = {"1": 248956422, "X": 156040895, "Y": 57227415, "MT": 16569}
T2T = {"1": 248387328, "X": 154259566, "Y": 62460029, "M": 16569}


def _header(lengths: dict[str, int], chr_prefix: bool = False, pad_to: int = 0) -> str:
    names = {(f"chr{n}" if chr_prefix else n): ln for n, ln in lengths.items()}
    for i in range(pad_to - len(names)):
        names[f"unplaced_{i}"] = 1000 + i
    return "".join(f"@SQ\tSN:{n}\tLN:{ln}\n" for n, ln in names.items())


def _identify(header: str, **kwargs):
    return identify_reference(header, calculate_bam_md5("", header=header), **kwargs)


def test_hs38d1s_signature_is_hg38():
    model = identify_reference("", "89cbafdbfcd4369f5aaa3bb34911125d")
    assert model is not None
    assert (model.name, model.fasta, model.evidence) == (
        "hs38d1s",
        "hs38d1s.fa.gz",
        "signature",
    )
    assert canonical_build(model.name) == "hg38"


def test_distributed_t2t_signature():
    model = identify_reference("", "ed9f80c8e04c461cba35dd1b85a2e2e8")
    assert model is not None and model.name == "T2Tv2.0"


@pytest.mark.parametrize(
    ("lengths", "build"), [(HG19, "hg19"), (HG38, "hg38"), (T2T, "T2T")]
)
@pytest.mark.parametrize("chr_prefix", [False, True])
def test_unknown_25_contig_dictionary_is_identified_by_chr1(lengths, build, chr_prefix):
    # 25 contigs fits hg19, hg38 and T2T alike, so the count must not decide.
    model = _identify(_header(lengths, chr_prefix, pad_to=25))
    assert model is not None
    assert (model.name, model.evidence) == (build, "chr1 length")
    assert model.naming == ("Chr" if chr_prefix else "Num")
    assert model.mito == "rCRS"
    assert model.fasta == ""


def test_unknown_assembly_with_25_contigs_is_not_hg19():
    header = _header({"chr1": 250_000_000, "chrX": 155_000_000}, pad_to=25)
    assert _identify(header) is None


def test_contig_count_names_the_catalog_fasta_only_when_builds_agree():
    model = _identify(_header(HG38, chr_prefix=True, pad_to=455))
    assert model is not None and model.fasta == "hg38.fa.gz"

    # 93 contigs is hg19's count, so it can't name a FASTA for an hg38 header.
    model = _identify(_header(HG38, chr_prefix=True, pad_to=93))
    assert model is not None and (model.name, model.fasta) == ("hg38", "")


def test_contig_count_identifies_pets_despite_a_chr1():
    model = _identify(_header({"1": 123_556_469, "X": 124_992_030}, pad_to=2197))
    assert model is not None
    assert model.name == "Dog_UU_GSD" and model.evidence == "contig count"


# 84 is human_g1k_v37's count, whose file name carries no build token.
@pytest.mark.parametrize("contigs", [455, 84])
def test_human_contig_count_is_ruled_out_by_an_unknown_chr1(contigs):
    assert _identify(_header({"chr1": 250_000_000}, pad_to=contigs)) is None


def test_reference_fasta_is_the_last_resort():
    header = _header({"contig_a": 1000, "contig_b": 2000})
    assert _identify(header) is None
    model = _identify(header, reference_fasta="/refs/hg38/hg19.fa.gz")
    assert model is not None and (model.name, model.evidence) == ("hg19", "reference")
    # A folder's name is not evidence about the file in it.
    assert _identify(header, reference_fasta="/refs/hg38/custom.fa") is None


def test_describe():
    model = _identify(_header(HG38, chr_prefix=True, pad_to=30))
    assert model is not None and model.describe() == "hg38 (Chr), rCRS, 30 SNs"


def _catalog() -> list[dict[str, str]]:
    path = resources.files("wgsextract_cli") / "assets/reference/seed_genomes.csv"
    with path.open() as f:
        return list(csv.DictReader(f))


# Ensembl's current GRCh38 download changes contigs between releases, so no one
# signature describes it; `ref verify` would reject a valid older download.
UNVERSIONED = {"GRCh38"}


@pytest.mark.parametrize(
    "row", _catalog(), ids=lambda r: f"{r['Pyth Code']}:{r['Source']}"
)
def test_every_catalog_genome_is_identified_by_its_signature(row):
    """A genome without a signature falls back to weaker evidence in `info`,
    and `ref verify` can't check it. Compute a new genome's signature with
    scripts/reference_signatures.py."""
    signature = row["Signature"]
    if row["Pyth Code"] in UNVERSIONED:
        assert not signature
        return
    assert signature in REFERENCE_MODELS, f"{signature!r} not in REFERENCE_MODELS"
    name = REFERENCE_MODELS[signature][0]
    assert canonical_build(name) == build_from_path(
        f"{row['Pyth Code']} {row['Final File Name']}"
    )


def test_catalog_signatures_reach_ref_verify():
    from wgsextract_cli.core.ref_library.catalog import load_genomes_from_csv

    path = resources.files("wgsextract_cli") / "assets/reference/seed_genomes.csv"
    genomes = {g["final"]: g for g in load_genomes_from_csv(str(path))}
    assert genomes["hs38d1s.fa.gz"]["md5"] == "89cbafdbfcd4369f5aaa3bb34911125d"


def test_vcf_contig_lines_are_read_when_there_are_no_sq_lines():
    header = (
        "##fileformat=VCFv4.2\n"
        "##contig=<ID=chr1,length=248387328>\n"
        "##contig=<ID=chrM,length=16569>\n"
    )
    model = identify_reference(header)
    assert model is not None
    assert (model.name, model.naming, model.mito) == ("T2T", "Chr", "rCRS")


def test_info_reads_the_model_name_with_or_without_a_naming():
    from wgsextract_cli.commands.info.runner import reference_model_name

    human = _identify(_header(HG38, chr_prefix=True, pad_to=30))
    cat = _identify(_header({"chrA1": 239_367_248, "chrA2": 169_388_855}, pad_to=71))
    assert human is not None and cat is not None and cat.naming == ""
    assert reference_model_name(human.describe()) == "hg38"
    assert reference_model_name(cat.describe()) == "Cat_Fca126"
    assert reference_model_name("Unknown, 12 SNs") == "Unknown"
