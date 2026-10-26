import functools
import gzip
import logging
import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TextIO, TypedDict

from pyliftover import LiftOver

from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.utils import WGSExtractError

TemplateSearchPath = str | os.PathLike[str]
TemplateSearchInput = TemplateSearchPath | Sequence[TemplateSearchPath | None] | None
_TEMPLATE_SEARCH_PARENT_LIMIT = 3


class _TemplateFormatOptions(TypedDict, total=False):
    # The template's positions are NCBI36 (hg18), not GRCh37: lifted to GRCh37
    # to find their calls, and written as they are.
    ncbi36: bool


class TemplateFormat(_TemplateFormatOptions):
    suffix: str
    parts: int


_COMPLEMENT = str.maketrans("ACGTacgt", "TGCAtgca")


def sort_alleles(genotype: str) -> str:
    """A two-allele call in alphabetical order (AG, never GA), as 23andMe,
    MyHeritage and upstream WGS Extract write them."""
    return "".join(sorted(genotype)) if len(genotype) == 2 else genotype


def chr_to_int(chrom: str | int) -> int:
    """
    Converts chromosome name to an integer for sorting.
    M/MT -> 23, X -> 24, Y -> 25.
    Ported from program/aconv.py chrconv().
    """
    c = str(chrom).upper().replace("CHR", "")
    if c == "M" or c == "MT":
        return 23
    if c == "X":
        return 24
    if c == "Y":
        return 25
    try:
        return int(c)
    except ValueError:
        return 99  # Unknown


# AncestryDNA numbers chrX 23, chrY 24, the X pseudoautosomal region 25 and
# chrMT 26; FTDNA names the pseudoautosomal region XY (at its chrX positions).
_VENDOR_CHROMS = {"23": "X", "24": "Y", "25": "X", "26": "MT", "XY": "X"}
# GRCh37's chrX pseudoautosomal regions, diploid in men too.
_X_PARS_GRCH37 = ((60001, 2699520), (154931044, 155260560))
_PACKAGED_TEMPLATES = (
    Path(__file__).resolve().parents[1] / "assets" / "microarray" / "raw_file_templates"
)


def vendor_chrom(chrom: str) -> str:
    """A template's chromosome as the kit names it (X, Y, MT, 1-22)."""
    return _VENDOR_CHROMS.get(chrom, chrom)


def _chrom_for_liftover(chrom: str) -> str:
    chrom = vendor_chrom(chrom)
    if chrom in {"M", "MT", "chrM", "chrMT"}:
        return "chrM"
    if chrom.startswith("chr"):
        return chrom
    return f"chr{chrom}"


def _chrom_from_liftover(chrom: str) -> str:
    if chrom in {"chrM", "chrMT", "M", "MT"}:
        return "MT"
    return chrom.removeprefix("chr")


def _iter_template_search_dirs(
    templates_dir: TemplateSearchInput,
) -> Iterator[str]:
    if not templates_dir:
        return

    search_dirs: Sequence[TemplateSearchPath | None]
    if isinstance(templates_dir, (str, os.PathLike)):
        search_dirs = (templates_dir,)
    else:
        search_dirs = templates_dir

    for search_dir in search_dirs:
        if search_dir:
            yield os.fspath(search_dir)


def _resolve_templates_roots(templates_dir: TemplateSearchInput) -> list[str]:
    """Resolve raw_file_templates roots from reference directories or nearby parents.

    Searches the provided directories plus a small number of parent directories
    for sibling ``microarray/raw_file_templates`` or ``raw_file_templates``
    directories. The bounded walk finds the reference-library layout without
    accidentally selecting unrelated templates from high-level filesystem roots.
    """
    roots: list[str] = []
    visited_dirs: set[str] = set()
    seen_roots: set[str] = set()

    for search_dir in _iter_template_search_dirs(templates_dir):
        current = str(absolute_path(search_dir))

        for _ in range(_TEMPLATE_SEARCH_PARENT_LIMIT + 1):
            if not current or current in visited_dirs:
                break
            visited_dirs.add(current)

            candidates = [
                str(Path(current, "microarray", "raw_file_templates")),
                str(Path(current, "raw_file_templates")),
            ]
            if Path(current.rstrip(os.sep)).name == "raw_file_templates":
                candidates.insert(0, current)

            for candidate in candidates:
                candidate = str(absolute_path(candidate))
                if Path(candidate).is_dir() and candidate not in seen_roots:
                    roots.append(candidate)
                    seen_roots.add(candidate)

            parent = str(Path(current).parent)
            if parent == current:
                break
            current = parent

    return roots


def _template_body_paths(
    templates_root: str, format_name: str, fmt_info: TemplateFormat
) -> list[str]:
    body_templates = []
    for i in range(1, fmt_info["parts"] + 1):
        part_suffix = f"_{i}" if fmt_info["parts"] > 1 else ""
        template_name = f"{format_name}{part_suffix}{fmt_info['suffix']}"
        path = Path(templates_root, "body", template_name)
        # Templates shipped with the CLI are gzipped (see assets/microarray).
        gzipped = path.with_name(path.name + ".gz")
        body_templates.append(str(gzipped if gzipped.exists() else path))
    return body_templates


def _open_template(path: str) -> TextIO:
    if path.endswith(".gz"):
        return gzip.open(path, "rt")
    return open(path)


@functools.cache
def _template_corrections() -> dict[tuple[str, str, str], str]:
    """{(template, ID, position): corrected position} from corrections.tsv."""
    corrections: dict[tuple[str, str, str], str] = {}
    with open(_PACKAGED_TEMPLATES / "corrections.tsv") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            template, snp_id, _chrom, pos, corrected = line.rstrip("\n").split("\t")
            corrections[(template, snp_id, pos)] = corrected
    return corrections


def template_rows(format_name: str, path: str) -> Iterator[tuple[str, str, str]]:
    """(ID, chromosome, position) of each marker in a template body, as the
    vendor names them, with corrections.tsv's fixes applied."""
    corrections = _template_corrections()
    with _open_template(path) as f:
        for line in f:
            line = line.strip().replace('"', "")
            if not line or line.startswith("#"):
                continue
            parts = line.split("," if line.count(",") > line.count("\t") else "\t")
            if len(parts) < 3:
                continue
            if format_name == "23andMe_SNPs_API":
                # This template is chrom (chr1), position, rsID.
                snp_id, chrom, pos = parts[2], _chrom_from_liftover(parts[0]), parts[1]
            else:
                snp_id, chrom, pos = parts[0], parts[1], parts[2]
            pos = corrections.get((format_name, snp_id, pos), pos)
            yield snp_id, chrom, pos


def ncbi36_to_grch37(lifter: LiftOver, chrom: str, pos: str) -> tuple[str, str] | None:
    """An NCBI36 template position on GRCh37 (vendor chromosome naming)."""
    if not pos.isdigit():
        return None
    hits = lifter.convert_coordinate(_chrom_for_liftover(chrom), int(pos) - 1)
    if not hits or "_" in hits[0][0]:
        return None
    return _chrom_from_liftover(hits[0][0]), str(hits[0][1] + 1)


def _resolve_templates_for_format(
    templates_dir: TemplateSearchInput, format_name: str, fmt_info: TemplateFormat
) -> tuple[str, list[str]]:
    roots = _resolve_templates_roots(templates_dir)
    if not roots:
        raise WGSExtractError(
            f"Microarray templates not found near {templates_dir!r}; "
            f"cannot generate {format_name} output."
        )

    first_missing: str | None = None
    for templates_root in roots:
        body_templates = _template_body_paths(templates_root, format_name, fmt_info)
        missing = [path for path in body_templates if not Path(path).exists()]
        if not missing:
            return templates_root, body_templates
        if first_missing is None:
            first_missing = missing[0]

    raise WGSExtractError(f"Template body not found: {first_missing}")


def liftover_hg38_to_hg19(
    input_txt: str,
    output_txt: str,
    chain_file: str,
    templates_dir: TemplateSearchInput = None,
) -> None:
    """
    Performs liftover from hg38 to hg19 using pyliftover.
    Ported from legacy program/hg38tohg19.py.
    """
    if not Path(chain_file).exists():
        raise FileNotFoundError(f"Liftover chain file not found: {chain_file}")

    lo = LiftOver(chain_file)
    bad_chrom = 0
    bad_pos = 0

    # Primary 25 sequences as in legacy code
    valid_chroms = [f"chr{i}" for i in range(1, 23)] + ["chrX", "chrY", "chrM"]

    headers: list[str] = []
    data: list[tuple[int, int, str]] = []

    with open(input_txt) as f_source:
        for line in f_source:
            if line.startswith("#"):
                headers.append(line)
                continue

            parts = line.strip().split("\t")
            if len(parts) < 4:
                continue

            snp_id, chrom, pos, result = parts[0], parts[1], parts[2], parts[3]

            try:
                # pyliftover works in 0-based positions.
                new_coord = lo.convert_coordinate(
                    _chrom_for_liftover(chrom), int(pos) - 1
                )
            except (ValueError, TypeError):
                continue

            if not new_coord:
                bad_pos += 1
                continue
            new_chrom, new_pos = new_coord[0][0], new_coord[0][1] + 1
            if new_coord[0][2:3] == ("-",):  # an inverted region: the other strand
                result = sort_alleles(result.translate(_COMPLEMENT))
            if new_chrom not in valid_chroms:
                bad_chrom += 1
                continue
            # Normalize back to legacy format (no 'chr', M->MT)
            out_chrom = _chrom_from_liftover(new_chrom)
            data.append(
                (
                    chr_to_int(out_chrom),
                    int(new_pos),
                    f"{snp_id}\t{out_chrom}\t{new_pos}\t{result}\n",
                )
            )

    if bad_chrom or bad_pos:
        logging.warning(
            f"Liftover partially failed: {bad_chrom} to AltContig, {bad_pos} not in new model"
        )

    data.sort(key=lambda row: (row[0], row[1]))

    with open(output_txt, "w", newline="\n") as f:
        # 1. Use preserved headers from source if any
        if headers:
            f.writelines(headers)
        # 2. Fallback: If no headers and templates_dir provided, try finding 23andMe_V3 head
        elif templates_dir:
            for templates_root in _resolve_templates_roots(templates_dir):
                head_path = str(Path(templates_root, "head", "23andMe_V3.txt"))
                if Path(head_path).exists():
                    with open(head_path) as f_h:
                        f.writelines(f_h.readlines())
                    break

        f.writelines(row[2] for row in data)


def get_template_format(format_name: str) -> TemplateFormat | None:
    """Returns metadata for known microarray formats."""
    # Ported from legacy program/aconv.py logic
    formats: dict[str, TemplateFormat] = {
        # v1 and v2 chips share one marker list (see assets/microarray).
        "23andMe_V2": {"suffix": ".txt", "parts": 1},
        "23andMe_V3": {"suffix": ".txt", "parts": 1},
        "23andMe_V4": {"suffix": ".txt", "parts": 2},
        "23andMe_V5": {"suffix": ".txt", "parts": 2},
        "23andMe_V5_2025": {"suffix": ".txt", "parts": 1},
        "Ancestry_V1": {"suffix": ".txt", "parts": 4},
        "Ancestry_V2": {"suffix": ".txt", "parts": 5},
        "Ancestry_V2_2024": {"suffix": ".txt", "parts": 1},
        "FTDNA_V1_Affy": {"suffix": ".csv", "parts": 1, "ncbi36": True},
        "FTDNA_V2": {"suffix": ".csv", "parts": 1},
        "FTDNA_V3": {"suffix": ".csv", "parts": 3},
        "MyHeritage_V1": {"suffix": ".csv", "parts": 1},
        "MyHeritage_V2": {"suffix": ".csv", "parts": 1},
        "23andMe_SNPs_API": {"suffix": ".txt", "parts": 1},
        "23andMe_V35": {"suffix": ".txt", "parts": 1},
        "LDNA_V1": {"suffix": ".txt", "parts": 1},
        "LDNA_V2": {"suffix": ".txt", "parts": 1},
        "Nucleus_V1": {"suffix": ".txt", "parts": 1},
    }
    return formats.get(format_name)


def write_formatted_line(
    f: TextIO,
    format_name: str,
    snp_id: str,
    chrom: str,
    pos: str,
    result: str,
    eol: str = "\n",
) -> None:
    """Writes a line in the specific vendor format. Ported from aconv.py."""

    if "Ancestry" in format_name:
        if result == "--":
            result = "00"
        # Ancestry expects tab separated alleles
        if len(result) == 2:
            val = f"{result[0]}\t{result[1]}"
        else:
            val = "0\t0"  # Fallback
        f.write(f"{snp_id}\t{chrom}\t{pos}\t{val}{eol}")

    elif "23andMe" in format_name:
        if chrom == "M":
            chrom = "MT"
        f.write(f"{snp_id}\t{chrom}\t{pos}\t{result}{eol}")

    elif format_name in ["MyHeritage_V2", "MyHeritage_V1"]:
        # MyHeritage V1 specific genotype swap
        if format_name == "MyHeritage_V1":
            if result == "CT":
                result = "TC"
            elif result == "GT":
                result = "TG"
        f.write(f'"{snp_id}","{chrom}","{pos}","{result}"{eol}')

    elif format_name == "FTDNA_V2":
        f.write(f'"{snp_id}","{chrom}","{pos}","{result}"{eol}')

    elif format_name in ("FTDNA_V1_Affy", "FTDNA_V3"):
        f.write(f"{snp_id},{chrom},{pos},{result}{eol}")

    else:
        # Generic fallback
        f.write(f"{snp_id}\t{chrom}\t{pos}\t{result}{eol}")


def _in_x_par(pos: str) -> bool:
    return pos.isdigit() and any(lo <= int(pos) <= hi for lo, hi in _X_PARS_GRCH37)


def _kit_is_male(calls: dict[tuple[str, str], str]) -> bool:
    """Whether the kit's chrX outside the PARs is (nearly) all homozygous, as a
    man's haploid X is; a woman's has thousands of heterozygous chip markers."""
    called = het = 0
    for (chrom, pos), genotype in calls.items():
        if chrom == "X" and not _in_x_par(pos) and genotype != "--":
            called += 1
            het += genotype[0] != genotype[-1]
    return called >= 100 and het < 0.05 * called


def _haploid_letter(genotype: str, chrom: str, pos: str, male: bool) -> str:
    """23andMe writes a haploid call as one letter: MT, Y, and a man's X
    outside the PARs."""
    haploid = chrom in ("MT", "M", "Y") or (
        chrom == "X" and male and not _in_x_par(pos)
    )
    if haploid and len(genotype) == 2 and genotype[0] == genotype[1] != "-":
        return genotype[0]
    return genotype


def convert_to_vendor_format(
    format_name: str,
    combined_kit_txt: str,
    output_path: str,
    templates_dir: TemplateSearchInput,
    ncbi36_chain: str | None = None,
) -> None:
    """
    Converts a CombinedKit.txt (GRCh37) to a vendor-specific format using
    templates. Ported from legacy program/aconv.py.

    An NCBI36 template (FTDNA's Affymetrix export) finds its calls at its
    markers' GRCh37 positions, lifted with ncbi36_chain (UCSC hg18ToHg19);
    without the chain, by rsID.
    """
    fmt_info = get_template_format(format_name)
    if not fmt_info:
        logging.warning(f"Unknown format: {format_name}, using generic fallback.")
        fmt_info = {"suffix": ".txt", "parts": 1}

    match_rsid = fmt_info.get("ncbi36", False) and not ncbi36_chain
    lifter = LiftOver(ncbi36_chain) if fmt_info.get("ncbi36") and ncbi36_chain else None
    if match_rsid:
        logging.warning(
            f"No hg18ToHg19 chain to place {format_name}'s NCBI36 markers on "
            "GRCh37; matching its markers by rsID instead."
        )
    called_variants: dict[tuple[str, str], str] = {}
    called_by_rsid: dict[str, str] = {}
    with open(combined_kit_txt) as f:
        for line in f:
            if line.startswith("#"):
                continue
            parts = line.strip().split("\t")
            if len(parts) >= 4:
                if match_rsid:
                    called_by_rsid[parts[0]] = parts[3]
                else:
                    called_variants[(str(parts[1]), str(parts[2]))] = parts[3]
    single_letter_haploid = format_name.startswith("23andMe")
    male = single_letter_haploid and _kit_is_male(called_variants)

    templates_root, body_templates = _resolve_templates_for_format(
        templates_dir, format_name, fmt_info
    )

    head_template = Path(templates_root, "head", f"{format_name}{fmt_info['suffix']}")
    head = head_template.read_bytes().decode() if head_template.exists() else ""
    # The body's line endings follow the head's (CRLF for 23andMe and AncestryDNA).
    eol = "\r\n" if head.endswith("\r\n") else "\n"
    # Write to a temp name so a failure never leaves a truncated vendor file.
    tmp_path = output_path + ".tmp"
    try:
        with open(tmp_path, "w", newline="") as f_out:
            f_out.write(head)
            for body_template in body_templates:
                for t_id, t_chrom, t_pos in template_rows(format_name, body_template):
                    if match_rsid:
                        result = called_by_rsid.get(t_id, "--")
                    else:
                        key: tuple[str, str] | None = (vendor_chrom(t_chrom), t_pos)
                        if lifter:
                            key = ncbi36_to_grch37(lifter, t_chrom, t_pos)
                        result = called_variants.get(key, "--") if key else "--"
                    if single_letter_haploid:
                        result = _haploid_letter(
                            result, vendor_chrom(t_chrom), t_pos, male
                        )
                    write_formatted_line(
                        f_out, format_name, t_id, t_chrom, t_pos, result, eol
                    )
        Path(tmp_path).replace(output_path)
    finally:
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()
