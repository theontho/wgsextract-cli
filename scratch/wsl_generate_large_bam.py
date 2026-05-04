import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wgsextract_cli.core.utils import run_command  # noqa: E402


def write_reference(ref_path: Path, seed: int) -> dict[str, int]:
    random.seed(seed)
    contigs = {"chr1": 10_000_000, "chrY": 1_000_000, "chrM": 16_569}
    bases = "ACGT"
    with ref_path.open("w", encoding="utf-8") as handle:
        for name, length in contigs.items():
            handle.write(f">{name}\n")
            for start in range(0, length, 80):
                chunk_len = min(80, length - start)
                handle.write("".join(random.choices(bases, k=chunk_len)) + "\n")
    return contigs


def write_sam(path: Path, contigs: dict[str, int], pairs: int, seed: int) -> None:
    random.seed(seed)
    contig_names = list(contigs)
    contig_weights = [contigs[name] for name in contig_names]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("@HD\tVN:1.6\tSO:unsorted\n")
        handle.write("@RG\tID:stress_rg\tSM:stress_sample\tPL:ILLUMINA\n")
        for name, length in contigs.items():
            handle.write(f"@SQ\tSN:{name}\tLN:{length}\n")
        for idx in range(pairs):
            contig = random.choices(contig_names, weights=contig_weights, k=1)[0]
            length = contigs[contig]
            start = random.randint(1, max(1, length - 500))
            insert_size = random.randint(250, 450)
            mate_start = min(start + insert_size - 100, length - 100)
            read_name = f"stress_{idx:08d}"
            read1_seq = "".join(random.choices("ACGT", k=100))
            read2_seq = "".join(random.choices("ACGT", k=100))
            handle.write(
                f"{read_name}\t99\t{contig}\t{start}\t60\t100M\t=\t{mate_start}\t{insert_size}\t"
                f"{read1_seq}\t{'I' * 100}\tRG:Z:stress_rg\n"
            )
            handle.write(
                f"{read_name}\t147\t{contig}\t{mate_start}\t60\t100M\t=\t{start}\t-{insert_size}\t"
                f"{read2_seq}\t{'I' * 100}\tRG:Z:stress_rg\n"
            )


def write_vcf(path: Path, contigs: dict[str, int], seed: int) -> None:
    random.seed(seed)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("##fileformat=VCFv4.2\n")
        handle.write('##FILTER=<ID=PASS,Description="All filters passed">\n')
        for name, length in contigs.items():
            handle.write(f"##contig=<ID={name},length={length}>\n")
        handle.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n')
        handle.write(
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tstress_sample\n"
        )
        for name, length in contigs.items():
            step = 10_000 if name != "chrM" else 500
            for pos in range(step, length, step):
                ref = random.choice("ACGT")
                alt = random.choice([base for base in "ACGT" if base != ref])
                handle.write(f"{name}\t{pos}\t.\t{ref}\t{alt}\t60\tPASS\t.\tGT\t0/1\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--pairs", type=int, default=350_000)
    parser.add_argument("--seed", type=int, default=20260503)
    args = parser.parse_args()

    start = time.perf_counter()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    ref = outdir / "fake_ref_hg38_scaled.fa"
    sam = outdir / "fake.sam"
    bam = outdir / "fake.bam"
    vcf = outdir / "fake.vcf"
    vcf_gz = outdir / "fake.vcf.gz"
    r1 = outdir / "fake_R1.fastq.gz"
    r2 = outdir / "fake_R2.fastq.gz"

    contigs = write_reference(ref, args.seed)
    run_command(["samtools", "faidx", str(ref)])

    write_sam(sam, contigs, args.pairs, args.seed + 1)
    run_command(
        [
            "samtools",
            "view",
            "-@",
            "2",
            "-b",
            str(sam),
            "-o",
            str(outdir / "fake_unsorted.bam"),
        ]
    )
    run_command(
        [
            "samtools",
            "sort",
            "-@",
            "2",
            "-m",
            "768M",
            "-o",
            str(bam),
            str(outdir / "fake_unsorted.bam"),
        ]
    )
    run_command(["samtools", "index", str(bam)])
    sam.unlink()
    (outdir / "fake_unsorted.bam").unlink()

    write_vcf(vcf, contigs, args.seed + 2)
    with vcf_gz.open("wb") as output:
        run_command(["bgzip", "-f", "-c", str(vcf)], stdout=output)
    run_command(["tabix", "-p", "vcf", str(vcf_gz)])
    vcf.unlink()

    run_command(
        [
            "samtools",
            "fastq",
            "-@",
            "2",
            "-1",
            str(r1),
            "-2",
            str(r2),
            "-0",
            "/dev/null",
            "-s",
            "/dev/null",
            str(bam),
        ]
    )

    elapsed = time.perf_counter() - start
    print(f"reference={ref} bytes={ref.stat().st_size}")
    print(f"bam={bam} bytes={bam.stat().st_size}")
    print(f"vcf={vcf_gz} bytes={vcf_gz.stat().st_size}")
    print(f"r1={r1} bytes={r1.stat().st_size}")
    print(f"pairs={args.pairs}")
    print(f"seconds={elapsed:.3f}")


if __name__ == "__main__":
    main()
