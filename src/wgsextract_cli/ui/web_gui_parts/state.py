"""Global state management for the Web GUI."""

import asyncio
import os
from pathlib import Path
from typing import Any

from ...core.config import settings


class State:
    def __init__(self):
        # Paths
        self.bam_path = settings.get("input_path", "")
        self.vcf_path = settings.get("default_input_vcf", "")
        self.fastq_path = ""

        # Auto-detect input types if VCF not set but input looks like VCF
        if not self.vcf_path and self.bam_path.lower().endswith(
            (".vcf", ".vcf.gz", ".bcf")
        ):
            self.vcf_path = self.bam_path
            self.bam_path = ""
        elif not self.fastq_path and self.bam_path.lower().endswith(
            (".fastq", ".fq", ".fastq.gz", ".fq.gz")
        ):
            self.fastq_path = self.bam_path
            self.bam_path = ""

        self.vcf_mother = settings.get("mother_vcf_path", "")
        self.vcf_father = settings.get("father_vcf_path", "")
        self.ref_path = settings.get("reference_library") or settings.get("reference_fasta", "")
        self.out_dir = settings.get("output_directory", "")
        self.yleaf_path = settings.get("yleaf_executable", "")
        self.yleaf_pos = ""
        self.haplogrep_path = settings.get("haplogrep_executable", "")

        # VCF Advanced
        self.vcf_ann_vcf = ""
        self.vcf_filter_expr = ""
        self.vcf_gene = ""
        self.vcf_region = ""
        self.vcf_vep_args = ""
        self.vep_cache_path = settings.get("vep_cache_directory", "")

        # Extract Advanced
        self.extract_region = ""
        self.extract_extra = ""

        # Pet Paths
        self.pet_species = "Select Pet Species..."
        self.pet_ref_fasta = ""
        self.pet_fastq_r1 = ""
        self.pet_fastq_r2 = ""
        self.pet_output_format = "BAM"

        # Options
        self.vcf_exclude_gaps = False
        self.cram_version = "3.0"

        # UI state
        self.active_tab = "flow"
        self.logs: dict[str, list[str]] = {"Main": []}
        self.log_tabs: list[str] = ["Main"]
        self.current_log_tab = "Main"
        self.running_processes: dict[str, asyncio.subprocess.Process] = {}
        self.active_downloads: dict[str, Any] = {}

    def get_info(self, path: str):
        """Placeholder for fast info update."""
        # This will be imported or injected to avoid circular dependency
        from .controller import controller

        if path and os.path.exists(path):
            asyncio.create_task(controller.get_info_fast(path))


state = State()
