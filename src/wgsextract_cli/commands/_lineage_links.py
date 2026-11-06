from urllib.parse import quote

from wgsextract_cli.core.results import human_output


def tree_links(
    haplogroup: str, *, mitochondrial: bool = False
) -> list[tuple[str, str]]:
    """(label, URL) of the public trees showing ``haplogroup``; real endpoints
    only. FamilyTreeDNA's mtDNA pages use Mitotree names, which match
    Haplogrep's ``mitotree-rcrs`` tree and often PhyloTree's."""
    value = quote(haplogroup.strip(), safe="")
    if not value or value == "NA":
        return []
    if mitochondrial:
        return [
            ("YFull mtDNA", f"https://www.yfull.com/mtree/{value}/"),
            ("FTDNA mtDNA", f"https://discover.familytreedna.com/mtdna/{value}/tree"),
        ]
    return [
        ("YFull", f"https://www.yfull.com/tree/{value}/"),
        ("FTDNA", f"https://discover.familytreedna.com/y-dna/{value}/tree"),
    ]


def print_tree_links(haplogroup: str, *, mitochondrial: bool = False) -> None:
    for label, url in tree_links(haplogroup, mitochondrial=mitochondrial):
        human_output(f"{label}: {url}")
