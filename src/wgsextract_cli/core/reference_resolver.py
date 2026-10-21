import functools
import logging
import os
import re
import subprocess
from pathlib import Path

from wgsextract_cli.core.builds import (
    HG37_BUILD_ALIASES,
    HG38_BUILD_ALIASES,
    build_from_chr1_length,
    build_from_path,
    canonical_build,
    is_hg37_build,
    is_hg38_build,
)
from wgsextract_cli.core.constants import REF_GENOME_FILENAMES
from wgsextract_cli.core.paths import PathInput, expand_path
from wgsextract_cli.core.reference_assets import (
    build_hint,
    find_fasta_by_signature,
    find_reference_fasta,
    find_reference_target_tab,
    is_fasta_path,
    read_fai_lengths,
    reference_dict_path,
    resolve_input_reference_fasta,
    resolve_input_target_tab,
)

from .alignment_metadata import (
    get_bam_header,
    get_vcf_build,
)

_UNKNOWN_SIGNATURE = "0" * 32
_VCF_CONTIG_RE = re.compile(r"^##contig=<.*?\bID=([^,>]+).*?\blength=(\d+)")


@functools.cache
def _warn_missing_configured_library(path: str) -> None:
    """Once per run: a configured library that isn't there finds no data."""
    from wgsextract_cli.core.config import get_config_path

    logging.warning(
        f"reference_library in {get_config_path()} is {path}, which doesn't "
        "exist. Point it at your reference library, or pass --ref."
    )


class ReferenceLibrary:
    """Helper to manage and resolve reference genome paths."""

    def __init__(
        self,
        root_path: PathInput | None,
        md5_sig: str | None = None,
        skip_full_search: bool = False,
        input_path: PathInput | None = None,
    ) -> None:
        from wgsextract_cli.core.config import settings

        root_path = expand_path(root_path) if root_path is not None else None
        input_path = expand_path(input_path) if input_path is not None else None
        self.root: str | None = root_path or settings.get("reference_library")
        if isinstance(self.root, str):
            self.root = expand_path(self.root)
            if not root_path and not Path(self.root).is_dir():
                _warn_missing_configured_library(self.root)
        self.md5: str | None = md5_sig
        self.input_path: str | None = input_path
        if isinstance(self.input_path, str):
            self.input_path = expand_path(self.input_path)
        self.fasta: str | None = None
        self.dict_file: str | None = None
        self.fai: str | None = None
        self.liftover_chain: str | None = None
        self.ref_vcf_tab: str | None = None
        self.clinvar_vcf: str | None = None
        self.revel_file: str | None = None
        self.phylop_file: str | None = None
        self.cadd_file: str | None = None
        self.cadd_indel_file: str | None = None
        self.gerp_file: str | None = None
        self.gnomad_vcf: str | None = None
        self.spliceai_vcf: str | None = None
        self.alphamissense_vcf: str | None = None
        self.pharmgkb_vcf: str | None = None
        self.ploidy_file: str | None = None
        self.vep_cache: str | None = None
        self.build: str | None = None

        if not self.root:
            return

        if Path(self.root).is_file() and is_fasta_path(self.root):
            self.fasta = self.root
            self.root = str(Path(self.root).parent)
            # A FASTA in <library>/genomes/ belongs to that library, whose root
            # holds the ploidy files, liftover chains and annotation data.
            if Path(self.root).name == "genomes":
                self.root = str(Path(self.root).parent)

        signature_match = False
        if not self.fasta:
            self.fasta = self._fasta_matching_signature()
            signature_match = bool(self.fasta)
        if not self.fasta:
            self.fasta = self._default_fasta(root_path)

        header_target = input_path or (
            root_path if root_path and Path(root_path).is_file() else None
        )
        self.build = self._detect_build(header_target, root_path)
        if not signature_match:
            self._reresolve_fasta_for_build()

        if not self.fasta:
            return

        self.fai = self.fasta + ".fai" if Path(self.fasta + ".fai").exists() else None
        dict_file = reference_dict_path(self.fasta)
        self.dict_file = dict_file if Path(dict_file).exists() else None

        self._resolve_build_support_files()
        self._resolve_vep_cache(settings.get("vep_cache_directory"))

        if skip_full_search:
            return

        self._resolve_ref_vcf_tab()
        self._resolve_annotation_files()

    # --- FASTA resolution -------------------------------------------------

    def _genome_dirs(self) -> list[str]:
        assert self.root is not None
        return [self.root, str(Path(self.root, "genomes"))]

    def _fasta_matching_signature(self) -> str | None:
        """Pick the library FASTA whose sequence dictionary matches the input's."""
        if not self.md5 or self.md5 == _UNKNOWN_SIGNATURE:
            return None
        fasta = find_fasta_by_signature(self._genome_dirs(), self.md5)
        if fasta:
            logging.debug(f"ReferenceLibrary: {fasta} matches signature {self.md5}")
        return fasta

    def _default_fasta(self, root_path: str | None) -> str | None:
        for search_dir in self._genome_dirs():
            if not Path(search_dir).is_dir():
                continue
            for file_name in REF_GENOME_FILENAMES.values():
                potential = str(Path(search_dir, file_name))
                if Path(potential).exists():
                    return potential
        assert self.root is not None
        return (
            find_reference_fasta(self.root, self.input_path, root_path)
            or resolve_input_reference_fasta(self.input_path)
            or None
        )

    def _reresolve_fasta_for_build(self) -> None:
        """Swap a path-guessed FASTA for one of the detected build when they disagree."""
        target = canonical_build(self.build)
        path_build = build_from_path(self.fasta) if self.fasta else None
        if not target or not path_build or path_build == target:
            return

        logging.debug(
            f"Build mismatch detected (Build={self.build}, Path={self.fasta}). Re-resolving FASTA..."
        )
        for search_dir in self._genome_dirs():
            if not Path(search_dir).is_dir():
                continue
            for key, file_name in REF_GENOME_FILENAMES.items():
                if build_from_path(f"{key} {file_name}") != target:
                    continue
                potential = str(Path(search_dir, file_name))
                if Path(potential).exists():
                    self.fasta = potential
                    return
        logging.warning(
            f"Could not find {self.build} genome, reverting to {self.fasta}"
        )

    # --- Build detection --------------------------------------------------

    def _detect_build(
        self, header_target: str | None, root_path: str | None
    ) -> str | None:
        build = self._build_from_signature()
        if not build and header_target:
            if header_target.lower().endswith((".vcf", ".vcf.gz", ".bcf")):
                build = get_vcf_build(header_target)
                if build:
                    logging.debug(
                        f"ReferenceLibrary: Identified build as {build} from VCF header"
                    )
            if not build:
                build = self._build_from_header(header_target)
        if not build and self.fasta:
            build = build_from_chr1_length(read_fai_lengths(self.fasta))
            build = build or build_from_path(self.fasta)
        assert self.root is not None
        return (
            build
            or build_from_path(self.root)
            or (build_from_path(self.input_path) if self.input_path else None)
            or build_hint(self.input_path, root_path)
            or None
        )

    def _build_from_signature(self) -> str | None:
        if not self.md5:
            return None
        from wgsextract_cli.core.constants import REFERENCE_MODELS

        model = REFERENCE_MODELS.get(self.md5)
        if not model:
            return None
        build = canonical_build(model[0]) or model[0]
        logging.debug(f"ReferenceLibrary: Identified build as {build} from MD5")
        return build

    @staticmethod
    def _build_from_header(header_target: str) -> str | None:
        try:
            header = get_bam_header(header_target)
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
            TypeError,
        ) as e:
            logging.debug(f"ReferenceLibrary: Error reading header: {e}")
            return None
        if not header:
            logging.debug(f"ReferenceLibrary: No header retrieved from {header_target}")
            return None

        lengths = header_contig_lengths(header)
        build = build_from_chr1_length(lengths)
        if not build:
            from wgsextract_cli.core.constants import REFGEN_BY_SNCOUNT

            entry = REFGEN_BY_SNCOUNT.get(len(lengths))
            build = build_from_path(str(entry[1])) if entry else None
        logging.debug(
            f"ReferenceLibrary: Identified build as {build} from {len(lengths)} header contigs"
        )
        return build

    # --- Companion resources ----------------------------------------------

    def _resolve_build_support_files(self) -> None:
        if not self.build:
            return
        assert self.root is not None
        root = self.root
        self.ploidy_file = self._first_existing_file(
            [root, str(Path(root, "ref")), str(Path(root, "microarray"))],
            [f"ploidy_{self.build}.txt", "ploidy.txt"],
        )

    def _resolve_vep_cache(self, configured_vep_cache: object) -> None:
        if (
            isinstance(configured_vep_cache, str)
            and Path(configured_vep_cache).is_dir()
        ):
            self.vep_cache = configured_vep_cache
            return
        assert self.root is not None
        for search_dir in [self.root, str(Path(self.root, "vep"))]:
            if Path(search_dir).is_dir() and any(
                f.endswith(("_GRCh38", "_GRCh37")) for f in os.listdir(search_dir)
            ):
                self.vep_cache = search_dir
                return
            vep_sub = str(Path(search_dir, "vep"))
            if Path(vep_sub).is_dir():
                self.vep_cache = vep_sub
                return

    def _resolve_ref_vcf_tab(self) -> None:
        """Microarray target: All_SNPs_<build>_ref.tab.gz wins over generic SNP VCFs.

        snps_<build>.vcf.gz in the bootstrap is a Y-SNP set, so it is only a last
        resort when no genome-wide target exists.
        """
        assert self.root is not None
        root = self.root
        search_dirs = [
            str(Path(root, "microarray")),
            root,
            str(Path(root, "ref")),
        ]
        tab_names: list[str] = []
        snp_vcf_names: list[str] = []
        if self.build:
            build = self.build.lower()
            alt_build = {"hg38": "grch38", "hg19": "grch37"}.get(build)
            spellings = [build, build.upper()]
            if alt_build:
                spellings += [alt_build, alt_build.upper(), f"GRCh{build[-2:]}"]
            tab_names = list(
                dict.fromkeys(f"All_SNPs_{name}_ref.tab.gz" for name in spellings)
            )
            snp_vcf_names = [f"snps_{build}.vcf.gz"]
            if alt_build:
                snp_vcf_names.append(f"snps_{alt_build}.vcf.gz")
            search_dirs += [
                str(Path(root, "microarray", self.build)),
                str(Path(root, self.build)),
                str(Path(root, "ref", self.build)),
            ]
            if alt_build:
                search_dirs += [
                    str(Path(root, alt_build)),
                    str(Path(root, "ref", alt_build)),
                ]
        generic_names = ["All_SNPs.vcf.gz", "common_all.vcf.gz"]
        walk_roots = [
            str(Path(root, "microarray")),
            str(Path(root, "ref")),
            str(Path(root, "genomes", "microarray")),
        ]

        self.ref_vcf_tab = (
            self._first_existing_file(search_dirs, tab_names)
            or self._walk_for_file(walk_roots, tab_names)
            or resolve_input_target_tab(self.input_path)
            or self._first_existing_file(search_dirs, generic_names)
            or find_reference_target_tab(root, self.build or "")
            or self._first_existing_file(search_dirs, snp_vcf_names)
            or self._walk_for_file(walk_roots, generic_names + snp_vcf_names)
        )

    def _resolve_annotation_files(self) -> None:
        from wgsextract_cli.core.config import settings

        assert self.root is not None
        dirs = [self.root, str(Path(self.root, "ref"))]
        vcf_exts = [".vcf.gz", ".vcf.bgz"]
        tsv_exts = [".tsv.gz", ".vcf.gz"]
        self.clinvar_vcf = self._resolve_annotation_file(
            settings.get("clinvar_vcf_path"), "clinvar", [".vcf.gz"], dirs
        )
        self.revel_file = self._resolve_annotation_file(
            settings.get("revel_tsv_path"), "revel", tsv_exts, dirs
        )
        self.phylop_file = self._resolve_annotation_file(
            settings.get("phylop_tsv_path"), "phylop", tsv_exts, dirs
        )
        self.cadd_file = self._resolve_annotation_file(
            settings.get("cadd_tsv_path"), "cadd", [".tsv.gz"], dirs
        )
        self.cadd_indel_file = self._resolve_annotation_file(
            settings.get("cadd_indel_tsv_path"), "cadd_indel", [".tsv.gz"], dirs
        )
        self.gerp_file = self._resolve_annotation_file(
            settings.get("gerp_bigwig_path"), "gerp", [".bw", ".bigWig"], dirs
        )
        # The v4.1 allele-frequency table (`ref gnomad --release 4.1`) wins over
        # the v2.1.1 exome VCF when both are installed.
        self.gnomad_vcf = self._resolve_annotation_file(
            settings.get("gnomad_vcf_path"),
            "gnomad",
            [".tsv.gz", ".vcf.bgz", ".vcf.gz"],
            dirs,
        )
        self.spliceai_vcf = self._resolve_annotation_file(
            settings.get("spliceai_vcf_path"), "spliceai", vcf_exts, dirs
        )
        self.alphamissense_vcf = self._resolve_annotation_file(
            settings.get("alphamissense_vcf_path"),
            "alphamissense",
            [".tsv.gz", *vcf_exts],
            dirs,
        )

        configured_pharmgkb = settings.get("pharmgkb_vcf_path")
        if isinstance(configured_pharmgkb, str) and Path(configured_pharmgkb).exists():
            self.pharmgkb_vcf = configured_pharmgkb
        elif self.build:
            pharmgkb_exts = [".vcf.gz", ".vcf.bgz", ".tsv.gz"]
            self.pharmgkb_vcf = self._resolve_annotation_file(
                None, "pharmgkb", pharmgkb_exts, dirs
            ) or self._first_existing_file(
                dirs, [f"pharmgkb{ext}" for ext in pharmgkb_exts]
            )

        if self.build and is_hg38_build(self.build):
            self.liftover_chain = self._first_existing_file(
                [*dirs, str(Path(self.root, "microarray"))],
                ["hg38ToHg19.over.chain.gz"],
            )

    @staticmethod
    def _first_existing_file(search_dirs: list[str], names: list[str]) -> str | None:
        for search_dir in search_dirs:
            if not Path(search_dir).is_dir():
                continue
            for name in names:
                potential = str(Path(search_dir, name))
                if Path(potential).exists():
                    return potential
        return None

    @staticmethod
    def _walk_for_file(search_roots: list[str], names: list[str]) -> str | None:
        for search_root in search_roots:
            if not Path(search_root).is_dir():
                continue
            for current_dir, _, files in os.walk(search_root):
                for name in names:
                    if name in files:
                        return str(Path(current_dir, name))
        return None

    def _resolve_annotation_file(
        self,
        configured_path: object,
        prefix: str,
        extensions: list[str],
        search_dirs: list[str],
    ) -> str | None:
        """Resolve an annotation file across directories and build aliases."""
        if isinstance(configured_path, str) and Path(configured_path).exists():
            return configured_path

        if not self.build:
            return None

        aliases = [self.build]
        if is_hg38_build(self.build):
            aliases.extend([*sorted(HG38_BUILD_ALIASES), "GRCh38"])
        elif is_hg37_build(self.build):
            aliases.extend([*sorted(HG37_BUILD_ALIASES), "GRCh37"])
        names = [f"{prefix}_{alias}{ext}" for alias in aliases for ext in extensions]
        names.extend(f"{prefix}{ext}" for ext in extensions)

        for search_dir in search_dirs:
            found = self._first_existing_file([search_dir], names)
            if found:
                return found
        return None


def header_contig_lengths(header: str) -> dict[str, int]:
    """Contig lengths from BAM @SQ lines, else from VCF ##contig lines."""
    lengths: dict[str, int] = {}
    for line in header.splitlines():
        if not line.startswith("@SQ"):
            continue
        fields = dict(
            part.split(":", 1) for part in line.split("\t")[1:] if ":" in part
        )
        if "SN" in fields and fields.get("LN", "").isdigit():
            lengths[fields["SN"]] = int(fields["LN"])
    if lengths:
        return lengths
    for line in header.splitlines():
        match = _VCF_CONTIG_RE.match(line)
        if match:
            lengths[match.group(1)] = int(match.group(2))
    return lengths
