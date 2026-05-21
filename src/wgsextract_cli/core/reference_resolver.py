import logging
import os

from wgsextract_cli.core.builds import (
    HG37_BUILD_ALIASES,
    HG38_BUILD_ALIASES,
    build_from_path,
    is_hg37_build,
    is_hg38_build,
)
from wgsextract_cli.core.constants import REF_GENOME_FILENAMES

try:
    import psutil
except ImportError:
    psutil = None


from .alignment_metadata import (
    get_bam_header,
    get_vcf_build,
)


class ReferenceLibrary:
    """Helper to manage and resolve reference genome paths."""

    def __init__(
        self,
        root_path: str | None,
        md5_sig: str | None = None,
        skip_full_search: bool = False,
        input_path: str | None = None,
    ):
        from wgsextract_cli.core.config import settings

        self.root: str | None = root_path or settings.get("reference_library")
        self.md5: str | None = md5_sig
        self.input_path: str | None = input_path
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
        self.mappability_map: str | None = None
        self.vep_cache: str | None = None
        self.build: str | None = None

        if not self.root:
            return

        if os.path.isfile(self.root):
            self.fasta = self.root
            self.root = os.path.dirname(self.root)

        d = self.root

        # Look for Fasta
        if not self.fasta:
            # Check direct directory and 'genomes' subdirectory
            for search_dir in [d, os.path.join(d, "genomes")]:
                if not os.path.isdir(search_dir):
                    continue
                for f in REF_GENOME_FILENAMES.values():
                    potential = os.path.join(search_dir, f)
                    if os.path.exists(potential):
                        self.fasta = potential
                        break
                if self.fasta:
                    break

        # Resolve associated files
        self.fai = self.fasta + ".fai" if self.fasta else None
        if self.fai and not os.path.exists(self.fai):
            self.fai = None

        # Build identification from MD5 (if provided)
        if self.md5:
            from wgsextract_cli.core.constants import REFERENCE_MODELS

            if self.md5 in REFERENCE_MODELS:
                self.build = REFERENCE_MODELS[self.md5][0]
                logging.debug(
                    f"ReferenceLibrary: Identified build as {self.build} from MD5"
                )
                # Normalize hs37d5 etc to hg19 for file lookups
                if self.build == "hs37d5":
                    self.build = "hg19"
                # Normalize hs38DH etc to hg38
                if self.build == "hs38DH":
                    self.build = "hg38"

        # Build identification from SN count (contig count) as fallback
        # Use input_path if provided, else root_path if it's a file
        target_for_header = (
            input_path
            if input_path
            else (root_path if root_path and os.path.isfile(root_path) else None)
        )
        if not self.build and target_for_header:
            if target_for_header.lower().endswith((".vcf", ".vcf.gz", ".bcf")):
                self.build = get_vcf_build(target_for_header)
                if self.build:
                    logging.debug(
                        f"ReferenceLibrary: Identified build as {self.build} from VCF header"
                    )

        if not self.build and target_for_header:
            try:
                header = get_bam_header(target_for_header)
                if header:
                    from wgsextract_cli.core.constants import REFGEN_BY_SNCOUNT

                    sq_lines = [
                        line for line in header.splitlines() if line.startswith("@SQ")
                    ]
                    sn_count = len(sq_lines)
                    logging.debug(f"ReferenceLibrary: Detected {sn_count} SQ lines")
                    if not sn_count:
                        # Try VCF contig count
                        sn_count = len(
                            [
                                line
                                for line in header.splitlines()
                                if line.startswith("##contig=")
                            ]
                        )
                        logging.debug(
                            f"ReferenceLibrary: Detected {sn_count} VCF contigs"
                        )

                    if sn_count in REFGEN_BY_SNCOUNT:
                        resolved_file = str(REFGEN_BY_SNCOUNT[sn_count][1]).lower()
                        if (
                            "37" in resolved_file
                            or "hg19" in resolved_file
                            or "hs37" in resolved_file
                        ):
                            self.build = "hg19"
                        elif (
                            "38" in resolved_file
                            or "hg38" in resolved_file
                            or "hs38" in resolved_file
                            or "grch38" in resolved_file
                        ):
                            self.build = "hg38"
                        else:
                            # Fallback to heuristics if filename is ambiguous
                            if sn_count > 190:
                                self.build = "hg38"
                            else:
                                self.build = "hg19"
                    elif sn_count > 190:  # Heuristic for hg38
                        self.build = "hg38"
                    elif sn_count > 80:  # Heuristic for hg19
                        self.build = "hg19"

                    logging.debug(
                        f"ReferenceLibrary: Identified build as {self.build} from SN count {sn_count}"
                    )
                else:
                    logging.debug(
                        f"ReferenceLibrary: No header retrieved from {target_for_header}"
                    )
            except Exception as e:
                logging.debug(f"ReferenceLibrary: Error reading header: {e}")

        # Build identification from path (fallback)
        if not self.build and self.fasta:
            self.build = build_from_path(self.fasta)

        if not self.build:
            self.build = build_from_path(d)

        if not self.build and self.input_path:
            self.build = build_from_path(self.input_path)

        # Re-resolve FASTA if build was found from MD5/Header but path-based resolution found something else
        if self.build and self.fasta:
            f_lower = self.fasta.lower()
            path_build = build_from_path(f_lower)
            is_hg38_path = is_hg38_build(path_build) if path_build else False
            is_hg19_path = is_hg37_build(path_build) if path_build else False

            mismatch = (is_hg38_build(self.build) and is_hg19_path) or (
                is_hg37_build(self.build) and is_hg38_path
            )

            if mismatch:
                logging.debug(
                    f"Build mismatch detected (Build={self.build}, Path={f_lower}). Re-resolving FASTA..."
                )
                original_fasta = self.fasta
                self.fasta = None
                # Check direct directory and 'genomes' subdirectory for the CORRECT build
                for search_dir in [d, os.path.join(d, "genomes")]:
                    if not os.path.isdir(search_dir):
                        continue
                    # Prioritize genomes that match our build
                    for build_key, f_name in REF_GENOME_FILENAMES.items():
                        # We only want to match the target build (hg38 or hg19)
                        match_hg19 = is_hg37_build(self.build) and (
                            "37" in build_key or "19" in build_key
                        )
                        match_hg38 = is_hg38_build(self.build) and (
                            "38" in build_key or "hs38" in build_key
                        )

                        if match_hg19 or match_hg38:
                            potential = os.path.join(search_dir, f_name)
                            if os.path.exists(potential):
                                self.fasta = potential
                                break
                    if self.fasta:
                        break

                if not self.fasta:
                    logging.warning(
                        f"Could not find {self.build} genome, reverting to {original_fasta}"
                    )
                    self.fasta = original_fasta

        if not self.fasta:
            return

        # Look for .dict
        self.dict_file = (
            self.fasta.replace(".fa.gz", ".dict")
            .replace(".fasta.gz", ".dict")
            .replace(".fa", ".dict")
            .replace(".fasta", ".dict")
        )
        if not os.path.exists(self.dict_file):
            self.dict_file = None

        # Look for ploidy
        if self.build:
            self.ploidy_file = self._first_existing_file(
                [
                    self.root,
                    os.path.join(self.root, "ref"),
                    os.path.join(self.root, "microarray"),
                ],
                [f"ploidy_{self.build}.txt", "ploidy.txt"],
            )

        # Look for Delly CNV mappability map
        if self.build:
            map_names = self._mappability_map_names()
            for search_dir in [
                self.root,
                os.path.join(self.root, "maps"),
                os.path.join(self.root, "ref"),
                os.path.join(self.root, "microarray"),
                os.path.join(self.root, self.build),
                os.path.join(self.root, "maps", self.build),
                os.path.join(self.root, "ref", self.build),
            ]:
                if not os.path.isdir(search_dir):
                    continue
                for name in map_names:
                    potential = os.path.join(search_dir, name)
                    if os.path.exists(potential):
                        self.mappability_map = potential
                        break
                if self.mappability_map:
                    break

        # Look for vep cache
        from wgsextract_cli.core.config import settings

        env_vep_cache = settings.get("vep_cache_directory")
        if env_vep_cache and os.path.isdir(env_vep_cache):
            self.vep_cache = env_vep_cache
        else:
            for search_dir in [self.root, os.path.join(self.root, "vep")]:
                if os.path.isdir(search_dir) and any(
                    f.endswith("_GRCh38") or f.endswith("_GRCh37")
                    for f in os.listdir(search_dir)
                ):
                    self.vep_cache = search_dir
                    break
                vep_sub = os.path.join(search_dir, "vep")
                if os.path.isdir(vep_sub):
                    self.vep_cache = vep_sub
                    break

        if skip_full_search:
            return

        # Annotation VCFs / Microarray Tab files
        potential_vcf_names = ["All_SNPs.vcf.gz", "common_all.vcf.gz"]
        if self.build:
            # e.g. All_SNPs_hg38_ref.tab.gz
            build_suffix = self.build.lower()  # hg38 or hg19
            # GRCh38 mapping
            alt_build = (
                "grch38"
                if build_suffix == "hg38"
                else "grch37"
                if build_suffix == "hg19"
                else None
            )

            potential_vcf_names.extend(
                [
                    f"snps_{build_suffix}.vcf.gz",
                    f"All_SNPs_{build_suffix}_ref.tab.gz",
                    f"All_SNPs_{build_suffix.upper()}_ref.tab.gz",
                    f"All_SNPs_GRCh{build_suffix[-2:]}_ref.tab.gz",
                    f"All_SNPs_grch{build_suffix[-2:]}_ref.tab.gz",
                ]
            )
            if alt_build:
                potential_vcf_names.extend(
                    [
                        f"snps_{alt_build.lower()}.vcf.gz",
                        f"All_SNPs_{alt_build.lower()}_ref.tab.gz",
                        f"All_SNPs_{alt_build.upper()}_ref.tab.gz",
                        f"All_SNPs_{alt_build.capitalize()}_ref.tab.gz",
                    ]
                )

        # Check in root, ref/, and microarray/ subdirectories, plus build-specific ones
        search_dirs = [
            self.root,
            os.path.join(self.root, "ref"),
            os.path.join(self.root, "microarray"),
        ]
        if self.build:
            search_dirs.extend(
                [
                    os.path.join(self.root, self.build),
                    os.path.join(self.root, "ref", self.build),
                    os.path.join(self.root, "microarray", self.build),
                ]
            )
            # Add alt build too
            if alt_build:
                search_dirs.extend(
                    [
                        os.path.join(self.root, alt_build),
                        os.path.join(self.root, "ref", alt_build),
                    ]
                )

        for search_dir in search_dirs:
            if not os.path.isdir(search_dir):
                continue
            for v in potential_vcf_names:
                potential = os.path.join(search_dir, v)
                if os.path.exists(potential):
                    self.ref_vcf_tab = potential
                    break
            if self.ref_vcf_tab:
                break

        if not self.ref_vcf_tab:
            support_search_roots = [
                os.path.join(self.root, "microarray"),
                os.path.join(self.root, "ref"),
                os.path.join(self.root, "genomes", "microarray"),
            ]
            for search_root in support_search_roots:
                if not os.path.isdir(search_root):
                    continue
                for current_dir, _, files in os.walk(search_root):
                    for v in potential_vcf_names:
                        if v in files:
                            self.ref_vcf_tab = os.path.join(current_dir, v)
                            break
                    if self.ref_vcf_tab:
                        break
                if self.ref_vcf_tab:
                    break

        # Look for ClinVar VCF
        self.clinvar_vcf = self._resolve_annotation_file(
            settings.get("clinvar_vcf_path"),
            "clinvar",
            [".vcf.gz"],
            [self.root, os.path.join(self.root, "ref")],
        )

        # Look for REVEL data
        self.revel_file = self._resolve_annotation_file(
            settings.get("revel_tsv_path"),
            "revel",
            [".tsv.gz", ".vcf.gz"],
            [self.root, os.path.join(self.root, "ref")],
        )

        # Look for PhyloP data
        self.phylop_file = self._resolve_annotation_file(
            settings.get("phylop_tsv_path"),
            "phylop",
            [".tsv.gz", ".vcf.gz"],
            [self.root, os.path.join(self.root, "ref")],
        )

        # Look for gnomAD VCF
        self.gnomad_vcf = self._resolve_annotation_file(
            settings.get("gnomad_vcf_path"),
            "gnomad",
            [".vcf.bgz", ".vcf.gz"],
            [self.root, os.path.join(self.root, "ref")],
        )

        # Look for SpliceAI VCF
        self.spliceai_vcf = self._resolve_annotation_file(
            settings.get("spliceai_vcf_path"),
            "spliceai",
            [".vcf.gz", ".vcf.bgz"],
            [self.root, os.path.join(self.root, "ref")],
        )

        # Look for AlphaMissense VCF
        self.alphamissense_vcf = self._resolve_annotation_file(
            settings.get("alphamissense_vcf_path"),
            "alphamissense",
            [".vcf.gz", ".vcf.bgz"],
            [self.root, os.path.join(self.root, "ref")],
        )

        # Look for PharmGKB VCF
        env_pharmgkb = settings.get("pharmgkb_vcf_path")
        if env_pharmgkb and os.path.exists(env_pharmgkb):
            self.pharmgkb_vcf = env_pharmgkb
        elif self.build:
            # PharmGKB has slightly different naming (can be prefix only)
            self.pharmgkb_vcf = self._resolve_annotation_file(
                None,
                "pharmgkb",
                [".vcf.gz", ".vcf.bgz", ".tsv.gz"],
                [self.root, os.path.join(self.root, "ref")],
            )
            if not self.pharmgkb_vcf:
                # Try prefix only
                for search_dir in [self.root, os.path.join(self.root, "ref")]:
                    if not os.path.isdir(search_dir):
                        continue
                    for ext in [".vcf.gz", ".vcf.bgz", ".tsv.gz"]:
                        potential = os.path.join(search_dir, f"pharmgkb{ext}")
                        if os.path.exists(potential):
                            self.pharmgkb_vcf = potential
                            break
                    if self.pharmgkb_vcf:
                        break

        # Look for Liftover Chain (hg38 -> hg19)
        if self.build and is_hg38_build(self.build):
            self.liftover_chain = self._first_existing_file(
                [
                    self.root,
                    os.path.join(self.root, "ref"),
                    os.path.join(self.root, "microarray"),
                ],
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

    def _mappability_map_names(self) -> list[str]:
        """Return build-compatible Delly map filenames in preference order."""
        if self.build and is_hg38_build(self.build):
            return [
                "hg38.map.gz",
                "grch38.map.gz",
                "GRCh38.map.gz",
                "Homo_sapiens.GRCh38.dna.primary_assembly.fa.r101.s501.blacklist.gz",
            ]
        if self.build and is_hg37_build(self.build):
            return [
                "hg19.map.gz",
                "grch37.map.gz",
                "GRCh37.map.gz",
                "Homo_sapiens.GRCh37.dna.primary_assembly.fa.r101.s501.blacklist.gz",
            ]
        return []

    def _resolve_annotation_file(
        self,
        env_path: str | None,
        prefix: str,
        extensions: list[str],
        search_dirs: list[str],
    ) -> str | None:
        """Helper to resolve a specific annotation file across multiple directories and build aliases."""
        if env_path and os.path.exists(env_path):
            return env_path

        if not self.build:
            return None

        for search_dir in search_dirs:
            if not os.path.isdir(search_dir):
                continue

            aliases = [self.build, ""]
            if is_hg38_build(self.build):
                aliases.extend(sorted(HG38_BUILD_ALIASES))
                aliases.append("GRCh38")
            elif is_hg37_build(self.build):
                aliases.extend(sorted(HG37_BUILD_ALIASES))
                aliases.append("GRCh37")
            for alt in aliases:
                alt_key = alt.lower()
                # Only check if it's potentially compatible with current build
                is_hg38_compatible = is_hg38_build(self.build) and is_hg38_build(
                    alt_key
                )
                is_hg19_compatible = is_hg37_build(self.build) and is_hg37_build(
                    alt_key
                )

                if is_hg38_compatible or is_hg19_compatible:
                    for ext in extensions:
                        potential = os.path.join(search_dir, f"{prefix}_{alt}{ext}")
                        if os.path.exists(potential):
                            return potential
                if alt == "":
                    for ext in extensions:
                        potential = os.path.join(search_dir, f"{prefix}{ext}")
                        if os.path.exists(potential):
                            return potential

        return None
