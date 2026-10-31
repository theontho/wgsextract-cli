import argparse
import gzip
import json
import tempfile
from pathlib import Path

from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.reference_assets import (
    read_fai_lengths,
    resolve_reference_library,
)
from wgsextract_cli.core.reference_models import sequence_lengths
from wgsextract_cli.core.utils import WGSExtractError, run_command, run_pipeline
from wgsextract_cli.core.variant_files import ensure_vcf_indexed, get_chr_name
from wgsextract_cli.core.ybrowse import (
    COPYRIGHT,
    SOURCE_URL,
    Y_LENGTH,
    installed_snapshot,
)

from ._extract_helpers import BaseArgs, cleanup_files, derive_output_path


def call_ybrowse(args: argparse.Namespace, base: BaseArgs) -> None:
    from .extract import _extract_regions, _warn_if_female, require_reference

    ref = require_reference(base.ref, "yBrowse Y calling", args.input)
    contigs = sequence_lengths(get_bam_header(args.input, base.cram_opt))
    chr_y = get_chr_name(args.input, "Y", base.cram_opt)
    lengths = read_fai_lengths(ref)
    if (
        contigs.get(chr_y) != Y_LENGTH
        or lengths.get(chr_y) != Y_LENGTH
        or sum(name in contigs for name in ("Y", "chrY")) != 1
        or any(
            sizes.get(name, 248_956_422) != 248_956_422
            for sizes in (contigs, lengths)
            for name in ("1", "chr1")
        )
    ):
        raise WGSExtractError(
            "--ybrowse requires an indexed, matching GRCh38 reference with chrY/Y "
            f"length {Y_LENGTH}. GRCh37 and T2T are unsupported."
        )
    directory, manifest = installed_snapshot(Path(resolve_reference_library()))
    _warn_if_female(args.input, base.outdir)
    output = derive_output_path(args.input, base.outdir, "_Y_ybrowse.vcf.gz")
    with tempfile.TemporaryDirectory(prefix=".ybrowse_", dir=base.outdir) as temporary:
        work = Path(temporary)
        normalized = work / "sites.vcf.gz"
        rename = work / "rename.tsv"
        rename.write_text(f"chrY\t{chr_y}\n", encoding="utf-8", newline="\n")
        # Fail on REF disagreement before decoding/calling the user's alignment.
        run_pipeline(
            [
                [
                    "bcftools",
                    "annotate",
                    "--rename-chrs",
                    str(rename),
                    "-Ou",
                    str(directory / "sites.vcf.gz"),
                ],
                [
                    "bcftools",
                    "norm",
                    "-c",
                    "e",
                    "-f",
                    ref,
                    "-Oz",
                    "-o",
                    str(normalized),
                ],
            ],
            description="Verify yBrowse REF alleles against GRCh38",
            stderr_dir=work,
        )
        ensure_vcf_indexed(str(normalized))
        bed = work / "sites.bed"
        targets = work / "targets.tsv"
        with (
            gzip.open(directory / "targets.tsv.gz", "rt", encoding="utf-8") as source,
            targets.open("w", encoding="utf-8", newline="\n") as target_out,
            bed.open("w", encoding="utf-8", newline="\n") as bed_out,
        ):
            for line in source:
                _, pos, alleles = line.rstrip("\n").split("\t")
                target_out.write(f"{chr_y}\t{pos}\t{alleles}\n")
                bed_out.write(f"{chr_y}\t{int(pos) - 1}\t{pos}\n")
        compressed_targets = work / "targets.tsv.gz"
        with compressed_targets.open("wb") as stream:
            run_command(["bgzip", "-c", str(targets)], stdout=stream)
        run_command(["tabix", "-s", "1", "-b", "2", "-e", "2", str(compressed_targets)])
        provenance = work / "provenance.txt"
        provenance.write_text(
            f'##copyright="{COPYRIGHT}"\n'
            f"##ybrowse_source={json.dumps(SOURCE_URL)}\n"
            f"##ybrowse_snapshot_sha256={manifest['source_sha256']}\n"
            f"##ybrowse_attribution={json.dumps(str(manifest['attribution']))}\n"
            "##ybrowse_scope=GRCh38_Y_SNPs_only;HG_is_ALT_annotation_not_sample_prediction\n",
            encoding="utf-8",
            newline="\n",
        )
        bam = str(work / "Y.bam")
        _extract_regions(base, args.input, bam, [chr_y], bed=str(bed))
        with cleanup_files(on_failure=[output, output + ".tbi", output + ".csi"]):
            run_pipeline(
                [
                    [
                        "bcftools",
                        "mpileup",
                        "-Ou",
                        "-f",
                        ref,
                        "-a",
                        "FORMAT/DP,FORMAT/AD",
                        "-T",
                        str(bed),
                        bam,
                    ],
                    [
                        "bcftools",
                        "call",
                        "-m",
                        "-A",
                        "-i",
                        "--ploidy",
                        "1",
                        "-C",
                        "alleles",
                        "-T",
                        str(compressed_targets),
                        "-Ou",
                    ],
                    [
                        "bcftools",
                        "norm",
                        "-m",
                        "-any",
                        "--multi-overlaps",
                        ".",
                        "-Ou",
                    ],
                    [
                        "bcftools",
                        "annotate",
                        "--pair-logic",
                        "exact",
                        "-a",
                        str(normalized),
                        "-c",
                        "ID,INFO/HG,INFO/ISOGG",
                        "-h",
                        str(provenance),
                        "-Oz",
                        "-o",
                        output,
                    ],
                ],
                description="Targeted haploid yBrowse calling and allele-aware annotation",
                stderr_dir=work,
            )
            ensure_vcf_indexed(output)
