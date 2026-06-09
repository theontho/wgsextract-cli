import logging
import os
from collections.abc import Iterator, Sequence
from typing import TextIO, TypedDict

from pyliftover import LiftOver

from wgsextract_cli.core.utils import WGSExtractError

TemplateSearchPath = str | os.PathLike[str]
TemplateSearchInput = TemplateSearchPath | Sequence[TemplateSearchPath | None] | None


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


def sort_microarray_file(input_file: str, output_file: str) -> None:
    """
    Sorts a microarray TSV file by chromosome and position.
    """
    data = []
    header = []
    with open(input_file) as f:
        for line in f:
            if line.startswith("#"):
                header.append(line)
                continue
            parts = line.strip().split("\t")
            if len(parts) >= 3:
                data.append(parts)

    # Sort by chromosome (using chr_to_int) and then position
    data.sort(key=lambda x: (chr_to_int(x[1]), int(x[2])))

    with open(output_file, "w") as f:
        for line in header:
            f.write(line)
        for parts in data:
            f.write("\t".join(parts) + "\n")


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
    if isinstance(templates_dir, str | os.PathLike):
        search_dirs = (templates_dir,)
    else:
        search_dirs = templates_dir

    for search_dir in search_dirs:
        if search_dir:
            yield os.fspath(search_dir)


def _resolve_templates_root(templates_dir: TemplateSearchInput) -> str | None:
    """Resolve the raw_file_templates root from a reference directory or its parents.

    Walks up the directory tree from ``templates_dir`` looking for a sibling
    ``microarray/raw_file_templates`` or ``raw_file_templates`` directory.
    Returns ``None`` when no templates can be located, so callers can report the
    missing dependency without silently searching the current working directory.
    """
    visited: set[str] = set()

    for search_dir in _iter_template_search_dirs(templates_dir):
        current = os.path.abspath(search_dir)

        while current and current not in visited:
            visited.add(current)

            candidates = [
                os.path.join(current, "microarray", "raw_file_templates"),
                os.path.join(current, "raw_file_templates"),
            ]
            if os.path.basename(current.rstrip(os.sep)) == "raw_file_templates":
                candidates.insert(0, current)

            for candidate in candidates:
                if os.path.isdir(candidate):
                    return candidate

            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

    return None


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

    # Use a temporary file for unsorted liftover results
    tmp_txt = output_txt + ".tmp"
    headers = []

    with open(tmp_txt, "w") as f_sink:
        with open(input_txt) as f_source:
            for line in f_source:
                if line.startswith("#"):
                    headers.append(line)
                    continue

                parts = line.strip().split("\t")
                if len(parts) < 4:
                    continue

                snp_id, chrom, pos, result = parts[0], parts[1], parts[2], parts[3]

                # Normalize chromosome name for pyliftover
                old_chrom = _chrom_for_liftover(chrom)

                try:
                    new_coord = lo.convert_coordinate(old_chrom, int(pos))
                except (ValueError, TypeError):
                    continue

                if new_coord:
                    new_chrom = new_coord[0][0]
                    new_pos = new_coord[0][1]

                    if new_chrom in valid_chroms:
                        # Normalize back to legacy format (no 'chr', M->MT)
                        out_chrom = _chrom_from_liftover(new_chrom)
                        f_sink.write(f"{snp_id}\t{out_chrom}\t{new_pos}\t{result}\n")
                    else:
                        bad_chrom += 1
                else:
                    bad_pos += 1

    if bad_chrom or bad_pos:
        logging.warning(
            f"Liftover partially failed: {bad_chrom} to AltContig, {bad_pos} not in new model"
        )

    # Now sort the temporary file and add the header
    data = []
    with open(tmp_txt) as f:
        for line in f:
            data.append(line.strip().split("\t"))

    data.sort(key=lambda x: (chr_to_int(x[1]), int(x[2])))

    with open(output_txt, "w") as f:
        # 1. Use preserved headers from source if any
        if headers:
            f.writelines(headers)
        # 2. Fallback: If no headers and templates_dir provided, try finding 23andMe_V3 head
        elif templates_dir:
            templates_root = _resolve_templates_root(templates_dir)
            if templates_root:
                head_path = os.path.join(templates_root, "head", "23andMe_V3.txt")
                if os.path.exists(head_path):
                    with open(head_path) as f_h:
                        f.writelines(f_h.readlines())

        for parts in data:
            f.write("\t".join(parts) + "\n")

    if os.path.exists(tmp_txt):
        os.remove(tmp_txt)


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

    elif format_name in ["FTDNA_V1_Affy", "MyHeritage_V2", "MyHeritage_V1"]:
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

    templates_root = _resolve_templates_root(templates_dir)
    if not templates_root:
        raise WGSExtractError(
            f"Microarray templates not found near {templates_dir!r}; "
            f"cannot generate {format_name} output."
        )

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

    body_templates = []
    for i in range(1, fmt_info["parts"] + 1):
        part_suffix = f"_{i}" if fmt_info["parts"] > 1 else ""
        template_name = f"{format_name}{part_suffix}{fmt_info['suffix']}"
        body_template = os.path.join(templates_root, "body", template_name)
        if not os.path.exists(body_template):
            raise WGSExtractError(f"Template body not found: {body_template}")
        body_templates.append(body_template)

    # Handle multiple parts (concatenated at the end)
    temp_files: list[str] = []
    for i, body_template in enumerate(body_templates, start=1):
        part_out = output_path + f".part{i}"
        temp_files.append(part_out)

        with open(part_out, "w") as f_out:
            with open(body_template) as f_temp:
                for line in f_temp:
                    line = line.strip().replace('"', "")
                    if not line:
                        continue

                    # Parse template line
                    parts = (
                        line.split(",")
                        if fmt_info["suffix"] == ".csv"
                        else line.split("\t")
                    )
                    if len(parts) < 3:
                        continue

                    # Templates are usually: ID, CHROM, POS
                    t_id, t_chrom, t_pos = parts[0], parts[1], parts[2]

                    # Lookup called result
                    result = called_variants.get((str(t_chrom), str(t_pos)), "--")
                    if "Ancestry" in format_name and result == "--":
                        result = "00"

                    write_formatted_line(
                        f_out, format_name, t_id, t_chrom, t_pos, result
                    )

    # Concatenate parts and add header
    head_template = os.path.join(
        templates_root, "head", f"{format_name}{fmt_info['suffix']}"
    )
    with open(output_path, "wb") as f_final:
        if os.path.exists(head_template):
            with open(head_template, "rb") as f_h:
                f_final.write(f_h.read())

        for part_file in temp_files:
            if os.path.exists(part_file):
                with open(part_file, "rb") as f_p:
                    f_final.write(f_p.read())
                os.remove(part_file)
