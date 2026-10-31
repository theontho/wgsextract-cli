from urllib.parse import quote


def print_tree_links(haplogroup: str, *, mitochondrial: bool = False) -> None:
    """Print real tree endpoints without inventing mtDNA branch URL parameters."""
    value = quote(haplogroup.strip(), safe="")
    if not value or value == "NA":
        return
    if mitochondrial:
        print(f"YFull mtDNA: https://www.yfull.com/mtree/{value}/")
        print("FTDNA mtDNA: https://www.familytreedna.com/public/mt-dna-haplotree")
    else:
        print(f"YFull: https://www.yfull.com/tree/{value}/")
        print(f"FTDNA: https://discover.familytreedna.com/y-dna/{value}/tree")
