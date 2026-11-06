"""Gene presets of ``panel bed``, each with the source its genes come from.

Symbols are HGNC's, as in the bundled NCBI RefSeq annotations
(assets/panel/genes_<build>.tsv.gz); tests check each one resolves in every
build. A ``Span`` is the stretch from one gene's start to another's end.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    name: str
    first: str
    last: str


@dataclass(frozen=True)
class Preset:
    name: str
    title: str
    source: str
    genes: tuple[str, ...] = ()
    spans: tuple[Span, ...] = ()
    acmg_category: str | None = None
    note: str = ""


# The classical MHC, class I to class II: GABBR1 to KIFC1 (Horton R et al.
# Nat Rev Genet 2004;5:889-899, doi:10.1038/nrg1489; Shiina T et al. J Hum
# Genet 2009;54:15-39, doi:10.1038/jhg.2008.5). About 3.8 Mb on GRCh38.
MHC = Span("MHC", "GABBR1", "KIFC1")

PRESETS: dict[str, Preset] = {
    preset.name: preset
    for preset in (
        Preset(
            "pharmacogenes",
            "Pharmacogenes",
            "PharmCAT's genes (https://pharmcat.clinpgx.org/Genes-Drugs/, "
            "CPIC guideline genes), with those it takes from outside callers.",
            genes=(
                # Called by PharmCAT from a VCF.
                "ABCG2",
                "CACNA1S",
                "CFTR",
                "CYP2B6",
                "CYP2C19",
                "CYP2C9",
                "CYP3A4",
                "CYP3A5",
                "CYP4F2",
                "DPYD",
                "F2",
                "F5",
                "G6PD",
                "IFNL3",
                "NAT2",
                "NUDT15",
                "RYR1",
                "SLCO1B1",
                "TPMT",
                "UGT1A1",
                "VKORC1",
                # Diplotypes from outside callers (typing cyp2d6, typing hla,
                # the mtDNA haplogroup for MT-RNR1).
                "CYP2D6",
                "HLA-A",
                "HLA-B",
                "MT-RNR1",
            ),  # fmt: skip
        ),
        Preset(
            "hla",
            "HLA / MHC",
            "The classical MHC, GABBR1 to KIFC1 (Horton 2004, "
            "doi:10.1038/nrg1489; Shiina 2009, doi:10.1038/jhg.2008.5).",
            spans=(MHC,),
        ),
        Preset(
            "autoimmune",
            "Autoimmune risk loci",
            "Genes of the 47 SNPs associated with several autoimmune diseases "
            "(Cotsapas C et al. PLoS Genet 2011;7(8):e1002254, Table 2, "
            "doi:10.1371/journal.pgen.1002254, CC BY 4.0), plus the MHC.",
            genes=(
                "IL23R",
                "CTLA4",
                "PTPN2",
                "IL18RAP",
                "CLEC16A",
                "ORMDL3",
                "STAT4",
                "CCR1",
                "IKZF1",
                "IL2",
                "IL21",
                "UBASH3A",
                "IRF5",
                "BACH2",
                "IL12B",
                "PRKCQ",
                "STAT3",
                "IL27",
                "HIC2",
                "UBE2L3",
                "CD226",
                "KIF21B",
                "ETS1",
                "JAK2",
                "LRRC18",
                "WDFY4",
                "CYP27B1",
                "IFIH1",
                "PTPN22",
                "SH2B3",
                "RGS1",
                "IL2RA",
                "IL10",
                "TNFAIP3",
                "PTGER4",
            ),  # fmt: skip
            spans=(MHC,),
            note="The table's five loci named only by cytoband (16p11.2, "
            "4p15.2, 5q31, 14q32.2, 7p15.2) are left out.",
        ),
        Preset(
            "acmg-sf",
            "ACMG secondary findings genes",
            "ACMG SF v3.2, all 81 genes (Miller DT et al. Genet Med "
            "2023;25(8):100866, doi:10.1016/j.gim.2023.100866).",
            acmg_category="",
        ),
        Preset(
            "cardiac",
            "Cardiac genes",
            "The cardiovascular genes of ACMG SF v3.2 (Miller DT et al. Genet "
            "Med 2023;25(8):100866, doi:10.1016/j.gim.2023.100866).",
            acmg_category="cardiovascular",
        ),
    )
}


def preset_genes(preset: Preset) -> tuple[str, ...]:
    """The preset's gene symbols (an ACMG preset's from the bundled list)."""
    if preset.acmg_category is None:
        return preset.genes
    from wgsextract_cli.core.personal_report import gene_list

    return tuple(
        gene.symbol
        for gene in gene_list()
        if not preset.acmg_category or gene.category == preset.acmg_category
    )
