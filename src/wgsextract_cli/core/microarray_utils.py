import logging
import os
from collections.abc import Iterator, Sequence
from typing import TextIO, TypedDict

from pyliftover import LiftOver

from wgsextract_cli.core.utils import WGSExtractError

TemplateSearchPath = str | os.PathLike[str]
TemplateSearchInput = TemplateSearchPath | Sequence[TemplateSearchPath | None] | None
_TEMPLATE_SEARCH_PARENT_LIMIT = 3


class TemplateFormat(TypedDict):
    suffix: str
    parts: int


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


def _chrom_for_liftover(chrom: str) -> str:
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
        current = os.path.abspath(search_dir)

        for _ in range(_TEMPLATE_SEARCH_PARENT_LIMIT + 1):
            if not current or current in visited_dirs:
                break
            visited_dirs.add(current)

            candidates = [
                os.path.join(current, "microarray", "raw_file_templates"),
                os.path.join(current, "raw_file_templates"),
            ]
            if os.path.basename(current.rstrip(os.sep)) == "raw_file_templates":
                candidates.insert(0, current)

            for candidate in candidates:
                candidate = os.path.abspath(candidate)
                if os.path.isdir(candidate) and candidate not in seen_roots:
                    roots.append(candidate)
                    seen_roots.add(candidate)

            parent = os.path.dirname(current)
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
        body_templates.append(os.path.join(templates_root, "body", template_name))
    return body_templates


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
        missing = [path for path in body_templates if not os.path.exists(path)]
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
    if not os.path.exists(chain_file):
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
                new_coord = lo.convert_coordinate(_chrom_for_liftover(chrom), int(pos))
            except (ValueError, TypeError):
                continue

            if not new_coord:
                bad_pos += 1
                continue
            new_chrom, new_pos = new_coord[0][0], new_coord[0][1]
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

    with open(output_txt, "w") as f:
        # 1. Use preserved headers from source if any
        if headers:
            f.writelines(headers)
        # 2. Fallback: If no headers and templates_dir provided, try finding 23andMe_V3 head
        elif templates_dir:
            for templates_root in _resolve_templates_roots(templates_dir):
                head_path = os.path.join(templates_root, "head", "23andMe_V3.txt")
                if os.path.exists(head_path):
                    with open(head_path) as f_h:
                        f.writelines(f_h.readlines())
                    break

        f.writelines(row[2] for row in data)


def get_template_format(format_name: str) -> TemplateFormat | None:
    """Returns metadata for known microarray formats."""
    # Ported from legacy program/aconv.py logic
    formats: dict[str, TemplateFormat] = {
        "23andMe_V3": {"suffix": ".txt", "parts": 1},
        "23andMe_V4": {"suffix": ".txt", "parts": 2},
        "23andMe_V5": {"suffix": ".txt", "parts": 2},
        "Ancestry_V1": {"suffix": ".txt", "parts": 4},
        "Ancestry_V2": {"suffix": ".txt", "parts": 5},
        "FTDNA_V2": {"suffix": ".csv", "parts": 1},
        "FTDNA_V3": {"suffix": ".csv", "parts": 3},
        "MyHeritage_V1": {"suffix": ".csv", "parts": 1},
        "MyHeritage_V2": {"suffix": ".csv", "parts": 1},
        "23andMe_SNPs_API": {"suffix": ".txt", "parts": 1},
        "23andMe_V35": {"suffix": ".txt", "parts": 1},
        "LDNA_V1": {"suffix": ".txt", "parts": 1},
        "LDNA_V2": {"suffix": ".txt", "parts": 1},
    }
    return formats.get(format_name)


def write_formatted_line(
    f: TextIO, format_name: str, snp_id: str, chrom: str, pos: str, result: str
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
        f.write(f"{snp_id}\t{chrom}\t{pos}\t{val}\n")

    elif "23andMe" in format_name or format_name == "23andMe_SNPs_API":
        if chrom == "M":
            chrom = "MT"
        f.write(f"{snp_id}\t{chrom}\t{pos}\t{result}\n")

    elif format_name in ["MyHeritage_V2", "MyHeritage_V1"]:
        # MyHeritage V1 specific genotype swap
        if format_name == "MyHeritage_V1":
            if result == "CT":
                result = "TC"
            elif result == "GT":
                result = "TG"
        f.write(f'"{snp_id}","{chrom}","{pos}","{result}"\n')

    elif format_name == "FTDNA_V2":
        f.write(f'"{snp_id}","{chrom}","{pos}","{result}"\n')

    elif format_name == "FTDNA_V3":
        f.write(f"{snp_id},{chrom},{pos},{result}\n")

    else:
        # Generic fallback
        f.write(f"{snp_id}\t{chrom}\t{pos}\t{result}\n")


def convert_to_vendor_format(
    format_name: str,
    combined_kit_txt: str,
    output_path: str,
    templates_dir: TemplateSearchInput,
) -> None:
    """
    Converts a CombinedKit.txt to a vendor-specific format using templates.
    Ported from legacy program/aconv.py.
    """
    fmt_info = get_template_format(format_name)
    if not fmt_info:
        logging.warning(f"Unknown format: {format_name}, using generic fallback.")
        fmt_info = {"suffix": ".txt", "parts": 1}

    # Load all called variants into memory for fast lookup
    called_variants: dict[tuple[str, str], str] = {}
    with open(combined_kit_txt) as f:
        for line in f:
            if line.startswith("#"):
                continue
            parts = line.strip().split("\t")
            if len(parts) >= 4:
                # Key is (chrom, pos)
                called_variants[(str(parts[1]), str(parts[2]))] = parts[3]

    templates_root, body_templates = _resolve_templates_for_format(
        templates_dir, format_name, fmt_info
    )

    head_template = os.path.join(
        templates_root, "head", f"{format_name}{fmt_info['suffix']}"
    )
    # Write to a temp name so a failure never leaves a truncated vendor file.
    # newline="" keeps LF line endings on every platform, as the legacy tool did.
    tmp_path = output_path + ".tmp"
    try:
        with open(tmp_path, "w", newline="") as f_out:
            if os.path.exists(head_template):
                with open(head_template, newline="") as f_h:
                    f_out.write(f_h.read())

            for body_template in body_templates:
                with open(body_template) as f_temp:
                    for line in f_temp:
                        line = line.strip().replace('"', "")
                        if not line:
                            continue

                        # Templates are: ID, CHROM, POS
                        parts = (
                            line.split(",")
                            if fmt_info["suffix"] == ".csv"
                            else line.split("\t")
                        )
                        if len(parts) < 3:
                            continue
                        t_id, t_chrom, t_pos = parts[0], parts[1], parts[2]

                        result = called_variants.get((t_chrom, t_pos), "--")
                        write_formatted_line(
                            f_out, format_name, t_id, t_chrom, t_pos, result
                        )
        os.replace(tmp_path, output_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
