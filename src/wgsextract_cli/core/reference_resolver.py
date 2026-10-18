import logging
import os
import re
import subprocess

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


class ReferenceLibrary:
    """Helper to manage and resolve reference genome paths."""

    def __init__(
        self,
        root_path: str | None,
        md5_sig: str | None = None,
        skip_full_search: bool = False,
        input_path: str | None = None,
    ) -> None:
        from wgsextract_cli.core.config import settings

        self.root: str | None = root_path or settings.get("reference_library")
        if isinstance(self.root, str):
            self.root = os.path.expanduser(self.root)
        self.md5: str | None = md5_sig
        self.input_path: str | None = input_path
        if isinstance(self.input_path, str):
            self.input_path = os.path.expanduser(self.input_path)
        self.fasta: str | None = None
        self.dict_file: str | None = None
        self.fai: str | None = None
        self.liftover_chain: str | None = None
        self.ref_vcf_tab: str | None = None
        self.clinvar_vcf: str | None = None
        self.revel_file: str | None = None
        self.phylop_file: str | None = None
        self.gnomad_vcf: str | None = None
        self.spliceai_vcf: str | None = None
        self.alphamissense_vcf: str | None = None
        self.pharmgkb_vcf: str | None = None
        self.ploidy_file: str | None = None
        self.vep_cache: str | None = None
        self.build: str | None = None

        if not self.root:
            return

        if os.path.isfile(self.root) and is_fasta_path(self.root):
            self.fasta = self.root
            self.root = os.path.dirname(self.root)
            # A FASTA in <library>/genomes/ belongs to that library, whose root
            # holds the ploidy files, liftover chains and annotation data.
            if os.path.basename(self.root) == "genomes":
                self.root = os.path.dirname(self.root)

        signature_match = False
        if not self.fasta:
            self.fasta = self._fasta_matching_signature()
            signature_match = bool(self.fasta)
        if not self.fasta:
            self.fasta = self._default_fasta(root_path)

        header_target = input_path or (
            root_path if root_path and os.path.isfile(root_path) else None
        )
        self.build = self._detect_build(header_target, root_path)
        if not signature_match:
            self._reresolve_fasta_for_build()

        if not self.fasta:
            return

        self.fai = self.fasta + ".fai" if os.path.exists(self.fasta + ".fai") else None
        dict_file = reference_dict_path(self.fasta)
        self.dict_file = dict_file if os.path.exists(dict_file) else None

        self._resolve_build_support_files()
        self._resolve_vep_cache(settings.get("vep_cache_directory"))

        if skip_full_search:
            return

        self._resolve_ref_vcf_tab()
        self._resolve_annotation_files()

    # --- FASTA resolution -------------------------------------------------

    def _genome_dirs(self) -> list[str]:
        assert self.root is not None
        return [self.root, os.path.join(self.root, "genomes")]

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
            if not os.path.isdir(search_dir):
                continue
            for file_name in REF_GENOME_FILENAMES.values():
                potential = os.path.join(search_dir, file_name)
                if os.path.exists(potential):
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
            if not os.path.isdir(search_dir):
                continue
            for key, file_name in REF_GENOME_FILENAMES.items():
                if build_from_path(f"{key} {file_name}") != target:
                    continue
                potential = os.path.join(search_dir, file_name)
                if os.path.exists(potential):
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

        lengths = _header_contig_lengths(header)
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
            [root, os.path.join(root, "ref"), os.path.join(root, "microarray")],
            [f"ploidy_{self.build}.txt", "ploidy.txt"],
        )

    def _resolve_vep_cache(self, configured_vep_cache: object) -> None:
        if isinstance(configured_vep_cache, str) and os.path.isdir(
            configured_vep_cache
        ):
            self.vep_cache = configured_vep_cache
            return
        assert self.root is not None
        for search_dir in [self.root, os.path.join(self.root, "vep")]:
            if os.path.isdir(search_dir) and any(
                f.endswith(("_GRCh38", "_GRCh37")) for f in os.listdir(search_dir)
            ):
                self.vep_cache = search_dir
                return
            vep_sub = os.path.join(search_dir, "vep")
            if os.path.isdir(vep_sub):
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
            os.path.join(root, "microarray"),
            root,
            os.path.join(root, "ref"),
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
                os.path.join(root, "microarray", self.build),
                os.path.join(root, self.build),
                os.path.join(root, "ref", self.build),
            ]
            if alt_build:
                search_dirs += [
                    os.path.join(root, alt_build),
                    os.path.join(root, "ref", alt_build),
                ]
        generic_names = ["All_SNPs.vcf.gz", "common_all.vcf.gz"]
        walk_roots = [
            os.path.join(root, "microarray"),
            os.path.join(root, "ref"),
            os.path.join(root, "genomes", "microarray"),
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
        dirs = [self.root, os.path.join(self.root, "ref")]
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
        self.gnomad_vcf = self._resolve_annotation_file(
            settings.get("gnomad_vcf_path"), "gnomad", [".vcf.bgz", ".vcf.gz"], dirs
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
        if isinstance(configured_pharmgkb, str) and os.path.exists(configured_pharmgkb):
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
                [*dirs, os.path.join(self.root, "microarray")],
                ["hg38ToHg19.over.chain.gz"],
            )

    @staticmethod
    def _first_existing_file(search_dirs: list[str], names: list[str]) -> str | None:
        for search_dir in search_dirs:
            if not os.path.isdir(search_dir):
                continue
            for name in names:
                potential = os.path.join(search_dir, name)
                if os.path.exists(potential):
                    return potential
        return None

    @staticmethod
    def _walk_for_file(search_roots: list[str], names: list[str]) -> str | None:
        for search_root in search_roots:
            if not os.path.isdir(search_root):
                continue
            for current_dir, _, files in os.walk(search_root):
                for name in names:
                    if name in files:
                        return os.path.join(current_dir, name)
        return None

    def _resolve_annotation_file(
        self,
        configured_path: object,
        prefix: str,
        extensions: list[str],
        search_dirs: list[str],
    ) -> str | None:
        """Resolve an annotation file across directories and build aliases."""
        if isinstance(configured_path, str) and os.path.exists(configured_path):
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


def _header_contig_lengths(header: str) -> dict[str, int]:
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
