import atexit
import logging
import os
import platform
import shutil
import subprocess

from wgsextract_cli.core.constants import REF_GENOME_FILENAMES
from wgsextract_cli.core.runtime import default_thread_tuning_profile

try:
    import psutil
except ImportError:
    psutil = None

import signal
import sys
import threading
import time


class WGSExtractError(Exception):
    """Base exception for wgsextract-cli errors."""

    pass


class ProcessRegistry:
    """
    Central registry for tracking sub-processes and cancel events.
    Enables reliable cleanup on application exit.
    """

    def __init__(self):
        self.processes: dict[str, subprocess.Popen] = {}
        self.events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def register_process(self, key: str, process: subprocess.Popen):
        with self._lock:
            self.processes[key] = process

    def unregister_process(self, key: str):
        with self._lock:
            if key in self.processes:
                del self.processes[key]

    def register_event(self, key: str, event: threading.Event):
        with self._lock:
            self.events[key] = event

    def unregister_event(self, key: str):
        with self._lock:
            if key in self.events:
                del self.events[key]

    def cleanup(self):
        """Terminate all registered processes and set all events."""
        with self._lock:
            # 1. Signal all events
            for event in self.events.values():
                event.set()

            # 2. Terminate all processes
            if not self.processes:
                return

            # Send termination signals
            for _key, proc in self.processes.items():
                if proc.poll() is None:
                    try:
                        if sys.platform == "win32":
                            # Windows: send CTRL_BREAK_EVENT to process group
                            # Use getattr to avoid MyPy error on non-Windows
                            proc.send_signal(
                                getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
                            )
                        else:
                            # Unix: kill the process group
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except Exception:
                        pass

            # Brief wait for graceful exit
            time.sleep(0.5)

            # Force kill any still alive
            for _key, proc in self.processes.items():
                if proc.poll() is None:
                    try:
                        if sys.platform == "win32":
                            proc.kill()
                        else:
                            os.killpg(
                                os.getpgid(proc.pid), getattr(signal, "SIGKILL", 9)
                            )
                    except Exception:
                        pass

            self.processes.clear()


proc_registry = ProcessRegistry()


def cleanup_processes():
    """Entry point for atexit and signal handlers."""
    proc_registry.cleanup()


atexit.register(cleanup_processes)


def _process_group_kwargs():
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def get_resource_defaults(threads_arg=None, memory_arg=None):
    """
    Calculate default CPU threads and memory if not provided.
    Logic inspired by Adjust_Mem in program/mainwindow.py.
    """
    # 1. Threads
    if threads_arg is not None:
        threads = str(threads_arg)
    else:
        threads = str(default_thread_tuning_profile().threads)

    # 2. Memory (in GB per thread for samtools sort)
    if memory_arg is not None:
        memory = str(memory_arg)
    else:
        # Default to 4GB total or 1GB per thread, whichever is smaller
        if psutil:
            total_mem_gb = psutil.virtual_memory().total / (1024**3)
            # Use 25% of system memory
            safe_mem = max(2, int(total_mem_gb * 0.25))
            # samtools sort -m is PER THREAD
            mem_per_thread = max(1, safe_mem // int(threads))
            memory = f"{mem_per_thread}G"
        else:
            memory = "1G"

    return threads, memory


def get_sam_sort_cmd(
    out_file,
    threads,
    memory,
    fmt="BAM",
    reference=None,
    name_sort=False,
    temp_dir=None,
):
    """
    Returns a command list for sorting BAM/CRAM.
    Uses sambamba if available (except on macOS) and format is BAM, else samtools.
    """
    threads_val = int(threads)
    # Convert memory (e.g. "1G") to just "1" for calculation
    mem_val = int(memory.rstrip("GgMm"))
    is_gb = memory.lower().endswith("g")

    is_macos = platform.system() == "Darwin"

    if shutil.which("sambamba") and fmt == "BAM" and not is_macos:
        # sambamba -m is TOTAL memory
        total_mem = mem_val * threads_val
        total_mem_str = f"{total_mem}G" if is_gb else f"{total_mem}M"
        cmd = [
            "sambamba",
            "sort",
            "-t",
            threads,
            "-m",
            total_mem_str,
            "-o",
            out_file,
            "/dev/stdin",
        ]
        if name_sort:
            cmd.insert(2, "-n")
        if temp_dir:
            cmd.insert(2, "--tmpdir")
            cmd.insert(3, temp_dir)
        return cmd
    else:
        # samtools sort -m is PER THREAD
        cmd = ["samtools", "sort", "-@", threads, "-m", memory, "-o", out_file]
        if name_sort:
            cmd.append("-n")
        if temp_dir:
            cmd += ["-T", temp_dir]
        if fmt == "CRAM":
            cmd += ["-O", "CRAM"]
            if reference:
                cmd += ["--reference", reference]
        elif fmt == "SAM":
            cmd += ["-O", "SAM"]
        else:
            cmd += ["-O", "BAM"]
        return cmd


def get_sam_index_cmd(file_path, threads="1"):
    """
    Returns a command list for indexing BAM/CRAM.
    Uses sambamba if available (except on macOS) and file is BAM, else samtools.
    """

    is_macos = platform.system() == "Darwin"

    if shutil.which("sambamba") and file_path.lower().endswith(".bam") and not is_macos:
        return ["sambamba", "index", "-t", threads, file_path]
    else:
        return ["samtools", "index", file_path]


def get_sam_view_cmd(threads="1", fmt="BAM", reference=None, is_input_sam=False):
    """
    Returns a command list for viewing/converting BAM/CRAM.
    Uses sambamba if available (except on macOS) and fmt is BAM, else samtools.
    """

    is_macos = platform.system() == "Darwin"

    if shutil.which("sambamba") and fmt == "BAM" and not reference and not is_macos:
        cmd = ["sambamba", "view", "-t", threads, "-f", "bam"]
        if is_input_sam:
            cmd += ["-S"]
        return cmd
    else:
        cmd = ["samtools", "view", "-@", threads]
        if fmt == "CRAM":
            cmd += ["-O", "CRAM"]
            if reference:
                cmd += ["-T", reference]
        elif fmt == "BAM":
            cmd += ["-b"]

        return cmd


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
                from wgsextract_cli.core.alignment_metadata import get_vcf_build

                self.build = get_vcf_build(target_for_header)
                if self.build:
                    logging.debug(
                        f"ReferenceLibrary: Identified build as {self.build} from VCF header"
                    )

        if not self.build and target_for_header:
            try:
                from wgsextract_cli.core.alignment_metadata import get_bam_header

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
            f_lower = self.fasta.lower()
            if "hg38" in f_lower or "grch38" in f_lower:
                self.build = "hg38"
            elif "hg19" in f_lower or "grch37" in f_lower or "hs37d5" in f_lower:
                self.build = "hg19"

        if not self.build:
            if "hg38" in d.lower() or "grch38" in d.lower():
                self.build = "hg38"
            elif "hg19" in d.lower() or "grch37" in d.lower():
                self.build = "hg19"
        if not self.build and self.input_path:
            input_lower = self.input_path.lower()
            if (
                "hg38" in input_lower
                or "grch38" in input_lower
                or "hs38" in input_lower
            ):
                self.build = "hg38"
            elif (
                "hg19" in input_lower
                or "grch37" in input_lower
                or "hs37" in input_lower
            ):
                self.build = "hg19"

        # Re-resolve FASTA if build was found from MD5/Header but path-based resolution found something else
        if self.build and self.fasta:
            f_lower = self.fasta.lower()
            is_hg38_path = "hg38" in f_lower or "grch38" in f_lower
            is_hg19_path = (
                "hg19" in f_lower or "grch37" in f_lower or "hs37d5" in f_lower
            )

            mismatch = (self.build == "hg38" and is_hg19_path) or (
                self.build == "hg19" and is_hg38_path
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
                        match_hg19 = self.build == "hg19" and (
                            "37" in build_key or "19" in build_key
                        )
                        match_hg38 = self.build == "hg38" and (
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
            for search_dir in [
                self.root,
                os.path.join(self.root, "ref"),
                os.path.join(self.root, "microarray"),
            ]:
                if not os.path.isdir(search_dir):
                    continue
                potential = os.path.join(search_dir, f"ploidy_{self.build}.txt")
                if os.path.exists(potential):
                    self.ploidy_file = potential
                    break
                # Generic name
                potential = os.path.join(search_dir, "ploidy.txt")
                if os.path.exists(potential):
                    self.ploidy_file = potential
                    break

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
        if self.build == "hg38":
            for search_dir in [
                self.root,
                os.path.join(self.root, "ref"),
                os.path.join(self.root, "microarray"),
            ]:
                if not os.path.isdir(search_dir):
                    continue
                potential = os.path.join(search_dir, "hg38ToHg19.over.chain.gz")
                if os.path.exists(potential):
                    self.liftover_chain = potential
                    break

    def _mappability_map_names(self) -> list[str]:
        """Return build-compatible Delly map filenames in preference order."""
        if self.build == "hg38":
            return [
                "hg38.map.gz",
                "grch38.map.gz",
                "GRCh38.map.gz",
                "Homo_sapiens.GRCh38.dna.primary_assembly.fa.r101.s501.blacklist.gz",
            ]
        if self.build in {"hg19", "GRCh37", "hs37d5"}:
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

            # Check direct build name and aliases
            aliases = [self.build, "hg38", "hg19", "grch38", "grch37", ""]
            for alt in aliases:
                # Only check if it's potentially compatible with current build
                is_hg38_compatible = self.build == "hg38" and (
                    alt == "hg38" or alt == "grch38"
                )
                is_hg19_compatible = self.build == "hg19" and (
                    alt == "hg19" or alt == "grch37"
                )

                if is_hg38_compatible or is_hg19_compatible:
                    for ext in extensions:
                        potential = os.path.join(search_dir, f"{prefix}_{alt}{ext}")
                        if os.path.exists(potential):
                            return potential
                # Handle cases without build suffix in filename (e.g. prefix.vcf.gz)
                if alt == "":
                    for ext in extensions:
                        potential = os.path.join(search_dir, f"{prefix}{ext}")
                        if os.path.exists(potential):
                            return potential

        return None


def resolve_reference(ref_path, md5_sig, input_path=None):
    """Find specific .fa.gz from directory or direct path."""
    lib = ReferenceLibrary(
        ref_path, md5_sig, skip_full_search=True, input_path=input_path
    )
    return lib.fasta if lib.fasta else ref_path


def calculate_bsd_sum(file_path):
    """
    Calculates BSD-style checksum and 1K block count.
    Used for local file comparison.
    """
    checksum = 0
    blocks = 0
    with open(file_path, "rb") as f:
        while chunk := f.read(1024):
            blocks += 1
            for byte in chunk:
                checksum = (checksum >> 1) + ((checksum & 1) << 15)
                checksum += byte
                checksum &= 0xFFFF
    return checksum, blocks


def _normalize_subprocess_cmd(cmd):
    """Expand shell-style command strings and configured Pixi tool wrappers."""
    import shlex

    from wgsextract_cli.core import (
        runtime,
        runtime_wrappers,
    )

    def split_wrapper_or_keep(value: str) -> list[str]:
        if (
            runtime.is_wsl_tool_command(value)
            or runtime.is_bundled_tool_command(value)
            or runtime.is_pacman_tool_command(value)
        ):
            return [value]
        if os.path.exists(value):
            return [value]
        if "pixi run" in value or " " in value:
            return shlex.split(value)
        return [value]

    if isinstance(cmd, str):
        cmd_list = shlex.split(cmd)
    else:
        cmd_list = []
        for item in cmd:
            if isinstance(item, str) and item == cmd[0]:
                cmd_list.extend(split_wrapper_or_keep(item))
            else:
                cmd_list.append(item)

    if cmd_list and isinstance(cmd_list[0], str):
        executable = cmd_list[0]
        if runtime.is_wsl_tool_command(executable):
            return runtime_wrappers.wrap_command(cmd_list)

        if executable and " " in executable and not os.path.exists(executable):
            cmd_list = shlex.split(executable) + cmd_list[1:]
            executable = cmd_list[0]

        if (
            executable
            and os.path.basename(executable) == executable
            and shutil.which(executable) is None
        ):
            from wgsextract_cli.core.dependencies import get_tool_path

            resolved = get_tool_path(executable)
            if resolved:
                if (
                    runtime.is_wsl_tool_command(resolved)
                    or runtime.is_bundled_tool_command(resolved)
                    or runtime.is_pacman_tool_command(resolved)
                ):
                    cmd_list = [resolved] + cmd_list[1:]
                elif os.path.exists(resolved):
                    cmd_list = [resolved] + cmd_list[1:]
                else:
                    cmd_list = shlex.split(resolved) + cmd_list[1:]

    return runtime_wrappers.wrap_command(cmd_list)


def run_command(
    cmd, capture_output=False, check=True, env=None, stdin=None, stdout=None
):
    """Helper to run subprocess with logging and registry."""
    cmd_list = _normalize_subprocess_cmd(cmd)

    cmd_str = " ".join(cmd_list)
    logging.debug(f"Running: {cmd_str}")

    # If stdout/stdin are provided, they take precedence over capture_output
    proc_stdout = (
        stdout if stdout is not None else (subprocess.PIPE if capture_output else None)
    )
    proc_stderr = subprocess.PIPE if capture_output else None

    process = subprocess.Popen(
        cmd_list,
        stdout=proc_stdout,
        stderr=proc_stderr,
        stdin=stdin,
        text=True if capture_output or (stdout is None) else False,
        env=env,
        **_process_group_kwargs(),
    )

    proc_registry.register_process(cmd_str, process)
    try:
        res_stdout, res_stderr = process.communicate()
        if check and process.returncode != 0:
            logging.error(f"Command failed: {cmd_str}")
            if res_stderr:
                logging.error(res_stderr)
            raise subprocess.CalledProcessError(
                process.returncode, cmd_list, res_stdout, res_stderr
            )
        return subprocess.CompletedProcess(
            cmd, process.returncode, res_stdout, res_stderr
        )
    finally:
        proc_registry.unregister_process(cmd_str)
