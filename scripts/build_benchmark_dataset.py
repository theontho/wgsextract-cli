#!/usr/bin/env python3
"""Build the release-backed real mini-genome benchmark dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

SAMPLE = "HG00096"
BUILD = "hg19"
DATASET_ID = "hg19-mini-hg00096"
ARCHIVE_NAME = "wgsextract-benchmark-hg19-mini.zip"
REFERENCE_URL = "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/technical/reference/phase2_reference_assembly_sequence/hs37d5.fa.gz"
BAM_URL = "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/phase3/data/HG00096/alignment/HG00096.mapped.ILLUMINA.bwa.GBR.low_coverage.20120522.bam"
VCF_URLS = {
    "20": "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/ALL.chr20.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf.gz",
    "Y": "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/ALL.chrY.phase3_integrated_v2b.20130502.genotypes.vcf.gz",
    "MT": "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/ALL.chrMT.phase3_callmom-v0_4.20130502.genotypes.vcf.gz",
}
REGIONS = {
    "20": (10_000_000, 11_000_000),
    "Y": (2_650_000, 3_150_000),
    "MT": (1, 16_569),
}
CONTIG_LENGTHS = {contig: end - start + 1 for contig, (start, end) in REGIONS.items()}
DEFAULT_REGION = "20"
LINE_WIDTH = 80
TARGET_LIMIT = 2_000
REQUIRED_TOOLS = ("samtools", "bcftools", "bgzip", "tabix")


@dataclass(frozen=True)
class Paths:
    workdir: Path
    dataset_dir: Path
    archive: Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--outdir",
        default="out/benchmark-dataset",
        help="Output directory for build intermediates and the zip archive.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing dataset build directory.",
    )
    args = parser.parse_args()

    verify_tools()
    paths = prepare_paths(Path(args.outdir), args.force)
    build_reference(paths.dataset_dir)
    build_alignment_files(paths.dataset_dir)
    build_vcf_files(paths.dataset_dir)
    build_targets(paths.dataset_dir)
    write_genome_config(paths.dataset_dir)
    write_manifest(paths.dataset_dir)
    write_checksums(paths.dataset_dir)
    create_archive(paths.dataset_dir, paths.archive)
    print(f"Archive: {paths.archive}")
    print(f"SHA256: {sha256(paths.archive)}")


def verify_tools() -> None:
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        raise SystemExit("Missing required tool(s): " + ", ".join(missing))


def prepare_paths(outdir: Path, force: bool) -> Paths:
    outdir = outdir.resolve()
    dataset_dir = outdir / DATASET_ID
    archive = outdir / ARCHIVE_NAME
    outdir.mkdir(parents=True, exist_ok=True)
    if dataset_dir.exists():
        if not force:
            raise SystemExit(f"Dataset directory already exists: {dataset_dir}")
        shutil.rmtree(dataset_dir)
    dataset_dir.mkdir(parents=True)
    if archive.exists() and force:
        archive.unlink()
    return Paths(outdir, dataset_dir, archive)


def build_reference(dataset_dir: Path) -> None:
    ref_plain = dataset_dir / "hg19-mini.fa"
    ref_gz = dataset_dir / "hg19-mini.fa.gz"
    sequences = {
        contig: fetch_reference_sequence(contig, start, end)
        for contig, (start, end) in REGIONS.items()
    }

    with open(ref_plain, "w", encoding="ascii", newline="\n") as handle:
        for contig in CONTIG_LENGTHS:
            handle.write(f">{contig}\n")
            sequence = sequences[contig]
            for line_start in range(0, len(sequence), LINE_WIDTH):
                handle.write(sequence[line_start : line_start + LINE_WIDTH] + "\n")

    run(["bgzip", "-f", str(ref_plain)])
    run(["samtools", "faidx", str(ref_gz)])
    run(["samtools", "dict", str(ref_gz), "-o", str(dataset_dir / "hg19-mini.dict")])


def fetch_reference_sequence(contig: str, start: int, end: int) -> str:
    result = run(
        ["samtools", "faidx", REFERENCE_URL, f"{contig}:{start}-{end}"],
        capture_output=True,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line]
    sequence = "".join(line for line in lines if not line.startswith(">"))
    expected = end - start + 1
    if len(sequence) != expected:
        raise SystemExit(
            f"Unexpected reference length for {contig}:{start}-{end}: {len(sequence)} != {expected}"
        )
    return sequence.upper()


def build_alignment_files(dataset_dir: Path) -> None:
    raw_bam = dataset_dir / "HG00096.hg19-mini.raw.bam"
    header_path = dataset_dir / "HG00096.hg19-mini.header.sam"
    rebased_sam = dataset_dir / "HG00096.hg19-mini.rebased.sam"
    rebased_bam = dataset_dir / "HG00096.hg19-mini.rebased.bam"
    bam = dataset_dir / "HG00096.hg19-mini.bam"
    cram = dataset_dir / "HG00096.hg19-mini.cram"
    ref = dataset_dir / "hg19-mini.fa.gz"

    region_args = [region_spec(contig) for contig in REGIONS]
    run(["samtools", "view", "-bh", "-o", str(raw_bam), BAM_URL, *region_args])

    header = run(["samtools", "view", "-H", str(raw_bam)], capture_output=True).stdout
    allowed = {f"SN:{contig}" for contig in CONTIG_LENGTHS}
    with open(header_path, "w", encoding="utf-8", newline="\n") as handle:
        for line in header.splitlines():
            if not line.startswith("@SQ") or any(
                token in line.split("\t") for token in allowed
            ):
                handle.write(sanitize_header_line(line) + "\n")

    write_rebased_sam(raw_bam, header_path, rebased_sam)
    run(["samtools", "view", "-b", "-o", str(rebased_bam), str(rebased_sam)])
    run(["samtools", "sort", "-o", str(bam), str(rebased_bam)])
    run(["samtools", "index", str(bam)])
    run(
        [
            "samtools",
            "view",
            "-C",
            "--output-fmt-option",
            "version=2.1",
            "-T",
            str(ref),
            "-o",
            str(cram),
            str(bam),
        ]
    )
    run(["samtools", "index", str(cram)])
    build_fastqs(dataset_dir, bam)

    raw_bam.unlink()
    header_path.unlink()
    rebased_sam.unlink()
    rebased_bam.unlink()


def write_rebased_sam(raw_bam: Path, header_path: Path, output_sam: Path) -> None:
    allowed = set(CONTIG_LENGTHS)
    records = run(["samtools", "view", str(raw_bam)], capture_output=True).stdout
    if not isinstance(records, str):
        raise SystemExit("samtools view returned binary output unexpectedly")
    with open(output_sam, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(header_path.read_text(encoding="utf-8"))
        for line in records.splitlines():
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) < 11:
                continue
            if fields[2] not in allowed:
                continue
            start, _end = REGIONS[fields[2]]
            fields[3] = str(int(fields[3]) - start + 1)
            if int(fields[3]) < 1:
                continue
            if fields[6] == "=":
                fields[7] = str(int(fields[7]) - start + 1)
            elif fields[6] in allowed:
                mate_start, _mate_end = REGIONS[fields[6]]
                fields[7] = str(int(fields[7]) - mate_start + 1)
            elif fields[6] != "*":
                fields[6] = "*"
                fields[7] = "0"
                fields[8] = "0"
            if int(fields[7]) < 1:
                fields[6] = "*"
                fields[7] = "0"
                fields[8] = "0"
            handle.write("\t".join(fields) + "\n")


def sanitize_header_line(line: str) -> str:
    if not line.startswith("@SQ"):
        return line
    fields = []
    for field in line.split("\t"):
        if field.startswith("M5:") or field.startswith("UR:"):
            continue
        if field.startswith("LN:"):
            sn = next(
                (part[3:] for part in line.split("\t") if part.startswith("SN:")), ""
            )
            field = f"LN:{CONTIG_LENGTHS[sn]}" if sn in CONTIG_LENGTHS else field
        fields.append(field)
    return "\t".join(fields)


def build_fastqs(dataset_dir: Path, bam: Path) -> None:
    name_sorted = dataset_dir / "HG00096.hg19-mini.name-sorted.bam"
    r1_plain = dataset_dir / "HG00096.hg19-mini_R1.fastq"
    r2_plain = dataset_dir / "HG00096.hg19-mini_R2.fastq"
    run(["samtools", "sort", "-n", "-o", str(name_sorted), str(bam)])
    run(
        [
            "samtools",
            "fastq",
            "-1",
            str(r1_plain),
            "-2",
            str(r2_plain),
            "-0",
            "/dev/null",
            "-s",
            "/dev/null",
            "-n",
            str(name_sorted),
        ]
    )
    run(["bgzip", "-f", str(r1_plain)])
    run(["bgzip", "-f", str(r2_plain)])
    name_sorted.unlink()


def build_vcf_files(dataset_dir: Path) -> None:
    temp_vcfs = []
    for contig, url in VCF_URLS.items():
        output = dataset_dir / f"HG00096.hg19-mini.{contig}.vcf.gz"
        source_contig = vcf_contig(url, contig)
        start, end = REGIONS[contig]
        run(
            [
                "bcftools",
                "view",
                "-s",
                SAMPLE,
                "-r",
                f"{source_contig}:{start}-{end}",
                "-Oz",
                "-o",
                str(output),
                url,
            ]
        )
        run(["tabix", "-f", "-p", "vcf", str(output)])
        temp_vcfs.append(output)

    vcf = dataset_dir / "HG00096.hg19-mini.vcf.gz"
    run(["bcftools", "concat", "-a", "-Oz", "-o", str(vcf), *map(str, temp_vcfs)])
    rebase_vcf(vcf)
    run(["tabix", "-f", "-p", "vcf", str(vcf)])
    for path in temp_vcfs:
        path.unlink()
        Path(str(path) + ".tbi").unlink()


def vcf_contig(url: str, preferred: str) -> str:
    contigs = run(["tabix", "-l", url], capture_output=True).stdout.splitlines()
    aliases = [preferred]
    if preferred == "MT":
        aliases.extend(["M", "chrM", "chrMT"])
    elif not preferred.startswith("chr"):
        aliases.append(f"chr{preferred}")
    for alias in aliases:
        if alias in contigs:
            return alias
    raise SystemExit(f"Could not find VCF contig for {preferred} in {url}: {contigs}")


def rebase_vcf(vcf_gz: Path) -> None:
    plain = vcf_gz.with_suffix("")
    result = run(["bcftools", "view", str(vcf_gz)], capture_output=True).stdout
    if not isinstance(result, str):
        raise SystemExit("bcftools view returned binary output unexpectedly")
    with open(plain, "w", encoding="utf-8", newline="\n") as handle:
        for line in result.splitlines():
            if line.startswith("##contig=<ID="):
                contig = line.split("ID=", 1)[1].split(",", 1)[0].split(">", 1)[0]
                if contig in CONTIG_LENGTHS:
                    handle.write(
                        f"##contig=<ID={contig},length={CONTIG_LENGTHS[contig]}>\n"
                    )
                continue
            if line.startswith("#"):
                handle.write(line + "\n")
                continue
            fields = line.split("\t")
            if fields[0] in REGIONS:
                start, _end = REGIONS[fields[0]]
                fields[1] = str(int(fields[1]) - start + 1)
            handle.write("\t".join(fields) + "\n")
    run(["bgzip", "-f", str(plain)])


def build_targets(dataset_dir: Path) -> None:
    vcf = dataset_dir / "HG00096.hg19-mini.vcf.gz"
    target_plain = dataset_dir / "HG00096.hg19-mini.targets.tab"
    query = run(
        [
            "bcftools",
            "query",
            "-f",
            "%CHROM\t%POS\t%ID\t%REF\t%ALT\n",
            "-i",
            'TYPE="snp" && N_ALT=1',
            str(vcf),
        ],
        capture_output=True,
    ).stdout
    count = 0
    with open(target_plain, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("#CHROM\tPOS\tID\tREF\tALT\n")
        for line in query.splitlines():
            if not line:
                continue
            chrom, pos, variant_id, ref, alt = line.split("\t")[:5]
            if variant_id == ".":
                variant_id = f"bench_{chrom}_{pos}"
            handle.write(f"{chrom}\t{pos}\t{variant_id}\t{ref}\t{alt}\n")
            count += 1
            if count >= TARGET_LIMIT:
                break
    if count == 0:
        raise SystemExit("No SNP targets were available in the mini VCF.")
    run(["bgzip", "-f", str(target_plain)])
    run(["tabix", "-f", "-p", "vcf", str(target_plain) + ".gz"])


def write_genome_config(dataset_dir: Path) -> None:
    (dataset_dir / "genome-config.toml").write_text(
        "\n".join(
            [
                "# WGS Extract benchmark mini genome dataset",
                'alignment = "HG00096.hg19-mini.bam"',
                'vcf = "HG00096.hg19-mini.vcf.gz"',
                'fastq_r1 = "HG00096.hg19-mini_R1.fastq.gz"',
                'fastq_r2 = "HG00096.hg19-mini_R2.fastq.gz"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_manifest(dataset_dir: Path) -> None:
    manifest = {
        "dataset_id": DATASET_ID,
        "description": "HG00096 real 1000 Genomes low-coverage mini benchmark dataset",
        "sample": SAMPLE,
        "build": BUILD,
        "default_region": DEFAULT_REGION,
        "source_reference": REFERENCE_URL,
        "source_alignment": BAM_URL,
        "source_vcfs": VCF_URLS,
        "regions": {
            contig: {
                "start": start,
                "end": end,
                "contig_length": CONTIG_LENGTHS[contig],
            }
            for contig, (start, end) in REGIONS.items()
        },
        "files": {
            "ref": "hg19-mini.fa.gz",
            "ref_fai": "hg19-mini.fa.gz.fai",
            "ref_gzi": "hg19-mini.fa.gz.gzi",
            "ref_dict": "hg19-mini.dict",
            "bam": "HG00096.hg19-mini.bam",
            "bam_index": "HG00096.hg19-mini.bam.bai",
            "cram": "HG00096.hg19-mini.cram",
            "cram_index": "HG00096.hg19-mini.cram.crai",
            "fastq_r1": "HG00096.hg19-mini_R1.fastq.gz",
            "fastq_r2": "HG00096.hg19-mini_R2.fastq.gz",
            "vcf": "HG00096.hg19-mini.vcf.gz",
            "vcf_index": "HG00096.hg19-mini.vcf.gz.tbi",
            "targets": "HG00096.hg19-mini.targets.tab.gz",
            "targets_index": "HG00096.hg19-mini.targets.tab.gz.tbi",
        },
    }
    (dataset_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def write_checksums(dataset_dir: Path) -> None:
    lines = []
    for path in sorted(p for p in dataset_dir.iterdir() if p.is_file()):
        if path.name == "SHA256SUMS":
            continue
        lines.append(f"{sha256(path)}  {path.name}")
    (dataset_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_archive(dataset_dir: Path, archive: Path) -> None:
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(dataset_dir.iterdir()):
            if path.is_file():
                zf.write(path, arcname=f"{dataset_dir.name}/{path.name}")


def region_spec(contig: str) -> str:
    start, end = REGIONS[contig]
    return f"{contig}:{start}-{end}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(
    command: list[str],
    *,
    capture_output: bool = False,
    stdout=None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    print("+ " + " ".join(command), flush=True)
    text = stdout is None
    return subprocess.run(
        command,
        check=True,
        text=text,
        capture_output=capture_output,
        stdout=stdout,
    )


if __name__ == "__main__":
    main()
