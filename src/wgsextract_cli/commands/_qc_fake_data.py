import logging
import os

from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    get_sam_index_cmd,
    get_sam_view_cmd,
    run_command,
)

from ._qc_bam_writer import (
    _create_fast_fake_bam,
    _write_fake_reference,
)
from ._qc_commands import (
    _reference_backed_sequence_provider,
)


def generate_fake_genomics_data(
    outdir: str,
    ref_path: str | None = None,
    coverage: float = 1.0,
    seed: int = 42,
    build: str = "hg38",
    full_size: bool = False,
    types: list[str] | None = None,
    target_md5: str | None = None,
    legacy_bam: bool = False,
) -> None:
    """Generates a scaled-down or full human fake BAM, CRAM and VCF."""
    import random

    if types is None:
        types = ["cram"]

    random.seed(seed)

    if legacy_bam and full_size:
        raise WGSExtractError("--legacy-bam is only supported for scaled fake data.")

    mode = "Full size" if full_size else "Scaled"
    logging.info(
        f"Generating fake human-like genomics data ({build}, {mode}) in {outdir} (Coverage: {coverage}x)..."
    )
    if target_md5:
        logging.debug(f"Generator using target MD5: {target_md5}")

    is_hg19 = build in ["hg19", "hg37"]

    # Pre-generate a noise buffer to use for both FASTA and BAM
    # 1MB of noise is enough to avoid obvious patterns
    noise_size = 1024 * 1024
    noise_buffer = "".join(random.choices(["A", "C", "G", "T"], k=noise_size))

    def get_noise_seq(chrom_idx, pos, length):
        # Use a large prime offset per chromosome to ensure diversity
        offset = chrom_idx * 15485863  # A large prime
        start = (pos + offset) % noise_size
        if start + length <= noise_size:
            return noise_buffer[start : start + length]
        else:
            # Wrap around
            return noise_buffer[start:] + noise_buffer[: length - (noise_size - start)]

    # Chromosome lengths
    if full_size:
        if build == "t2t":
            chroms = {
                "chr1": 248387328,
                "chr2": 242696752,
                "chr3": 201105948,
                "chr4": 193574945,
                "chr5": 182045439,
                "chr6": 172126628,
                "chr7": 160567428,
                "chr8": 146259331,
                "chr9": 150617247,
                "chr10": 134758134,
                "chr11": 135127769,
                "chr12": 133324548,
                "chr13": 113566686,
                "chr14": 101161492,
                "chr15": 99753195,
                "chr16": 96330374,
                "chr17": 84276017,
                "chr18": 80542538,
                "chr19": 61707364,
                "chr20": 66210255,
                "chr21": 45090682,
                "chr22": 57938617,
                "chrX": 154259566,
                "chrY": 62460029,
                "chrM": 16569,
            }
        elif is_hg19:
            chroms = {
                "1": 249250621,
                "2": 243199373,
                "3": 198022430,
                "4": 191154276,
                "5": 180915260,
                "6": 171115067,
                "7": 159138663,
                "8": 146364022,
                "9": 141213431,
                "10": 135534747,
                "11": 135006516,
                "12": 133851895,
                "13": 115169878,
                "14": 107349540,
                "15": 102531392,
                "16": 90354753,
                "17": 81195210,
                "18": 78077248,
                "19": 59128983,
                "20": 63025520,
                "21": 48129895,
                "22": 51304566,
                "X": 155270560,
                "Y": 59373566,
                "MT": 16569,
            }
        else:
            chroms = {
                "chr1": 248956422,
                "chr2": 242193529,
                "chr3": 198295559,
                "chr4": 190214555,
                "chr5": 181538259,
                "chr6": 170805979,
                "chr7": 159345973,
                "chr8": 145138636,
                "chr9": 138394717,
                "chr10": 133797422,
                "chr11": 135086622,
                "chr12": 133275309,
                "chr13": 114364328,
                "chr14": 107043718,
                "chr15": 101991189,
                "chr16": 90338345,
                "chr17": 83257441,
                "chr18": 80373285,
                "chr19": 58617616,
                "chr20": 64444167,
                "chr21": 46709983,
                "chr22": 50818468,
                "chrX": 156040895,
                "chrY": 57227415,
                "chrM": 16569,
            }

        # Calculate estimated BAM size to warn user
        # 1x WGS BAM is roughly 3GB.
        est_bam_gb = 3 * coverage
        if est_bam_gb > 1:
            logging.warning(
                f"Generating {est_bam_gb:.1f}GB of fake data. This will take significant time and disk space."
            )
            # Check free space
            import shutil

            _, _, free = shutil.disk_usage(outdir)
            if free < (est_bam_gb * 1.5) * (1024**3):
                msg = f"Insufficient disk space in {outdir}. Need at least {est_bam_gb * 1.5:.1f}GB."
                logging.error(msg)
                raise WGSExtractError(msg)
    else:
        chroms = {}
        for i in range(1, 23):
            name = f"chr{i}" if not is_hg19 else str(i)
            chroms[name] = 500000 + (i * 1000)  # variety
        chroms["chrX" if not is_hg19 else "X"] = 600000
        chroms["chrY" if not is_hg19 else "Y"] = 400000
        chroms["chrM" if not is_hg19 else "MT"] = 16569  # Real length for chrM

    # Add a dummy contig to avoid collision with known SN counts in info.py (e.g. 25)
    chroms["chrExtra" if not is_hg19 else "Extra"] = 1000

    # 0. Pre-generate consistent variants only for the legacy scaled BAM path.
    # The default streaming path applies deterministic SNPs in-flight without a
    # chromosome-wide variant map so it works at full-genome scale.
    need_bam = any(t in types for t in ["bam", "cram", "fastq"])
    use_streaming_bam = need_bam and not legacy_bam
    consistent_variants = {}
    if need_bam and not use_streaming_bam:
        # This ensures that all reads covering a position see the same variant
        # variants[chrom] = {pos: (ref, alt, is_indel, cigar_change)}
        for name, length in chroms.items():
            v_list = {}
            # 1 variant every 2000 bp
            num_v = max(2, length // 2000)
            for _ in range(num_v):
                v_pos = random.randint(100, length - 100)
                v_type = random.random()
                if v_type < 0.8:  # SNP
                    v_list[v_pos] = (
                        random.choice("ACGT"),
                        random.choice("ACGT"),
                        False,
                    )
                else:  # Indel (just a marker for now, we'll do real ones if possible)
                    v_list[v_pos] = (random.choice("ACGT"), "AT", True)
            consistent_variants[name] = v_list

    ref_path_was_provided = bool(ref_path)

    # 1. Create a reference if none provided
    if not ref_path:
        ref_path = os.path.join(
            outdir, f"fake_ref_{build}_{mode.lower().replace(' ', '_')}.fa"
        )

    # Preserve scaled fake-data behavior by creating a small reference, but avoid
    # writing a full human FASTA unless a caller explicitly requested it or CRAM
    # output requires a reference.
    should_create_reference = ref_path_was_provided or not full_size or "cram" in types
    ref_exists = os.path.exists(str(ref_path))
    if ref_exists:
        logging.info(f"Using reference: {ref_path}")
    elif should_create_reference:
        logging.info(f"Creating fake reference at {ref_path}...")
        _write_fake_reference(str(ref_path), chroms, get_noise_seq)
        run_command(["samtools", "faidx", ref_path])
    else:
        logging.info(
            "Skipping full-size fake reference creation because the requested outputs "
            "do not require a reference."
        )

    # 2. Create fake BAM with reads on all chromosomes based on coverage
    # We generate reads in sorted order to avoid a massive sort operation
    sam_path = os.path.join(outdir, "fake.sam")
    bam_path = os.path.join(outdir, "fake.bam")
    base_read_len = 100
    base_insert_size = 300

    if need_bam:
        if use_streaming_bam:
            threads, _ = get_resource_defaults(None, None)
            _create_fast_fake_bam(
                bam_path,
                chroms,
                coverage,
                seed,
                target_md5,
                _reference_backed_sequence_provider(ref_path, chroms, get_noise_seq),
                threads,
            )
        else:
            with open(sam_path, "w") as f:
                f.write("@HD\tVN:1.6\tSO:coordinate\n")

                # Embed MD5 in Read Group Description so it survives into BAM and is visible to samtools view -H
                rg_line = "@RG\tID:sample1\tSM:sample1\tPL:ILLUMINA"
                if target_md5:
                    rg_line += f"\tDS:MD5:{target_md5}"
                f.write(rg_line + "\n")

                if target_md5:
                    f.write(f"@CO\tMD5:{target_md5}\n")

                # Always write SQ lines from chroms dict for fake data
                for name, length in chroms.items():
                    f.write(f"@SQ\tSN:{name}\tLN:{length}\n")

                # Generate reads chromosome by chromosome (sorted)
                for idx, (name, length) in enumerate(chroms.items()):
                    num_pairs = int(
                        (length * coverage) / (base_read_len * 2)
                    )  # divided by 2 for pairs
                    if num_pairs < 2:
                        num_pairs = 2

                    # Generate read pairs
                    reads = []
                    cv = consistent_variants.get(name, {})
                    for i in range(num_pairs):
                        # Randomize read length and insert size
                        rl1 = int(random.gauss(base_read_len, 2))
                        rl2 = int(random.gauss(base_read_len, 2))
                        # Ensure lengths are reasonable
                        rl1 = max(50, min(150, rl1))
                        rl2 = max(50, min(150, rl2))

                        ins = int(random.gauss(base_insert_size, 15))
                        ins = max(rl1 + rl2 + 10, ins)  # Ensure no weird overlaps

                        pos1 = random.randint(1, length - ins - 100)
                        pos2 = pos1 + ins - rl2

                        read_id = f"read_{name}_{i}"

                        # Pull sequences from the deterministic noise buffer
                        r1_seq_list = list(get_noise_seq(idx, pos1 - 1, rl1))
                        r2_seq_list = list(get_noise_seq(idx, pos2 - 1, rl2))

                        # Apply consistent variants
                        # Homozygous for smoke tests to ensure reliable calling
                        r1_cigar = f"{rl1}M"
                        r2_cigar = f"{rl2}M"

                        # Check R1
                        for v_pos, v_data in cv.items():
                            _v_ref, v_val, is_indel = v_data
                            if pos1 <= v_pos < pos1 + rl1:
                                rel_pos = v_pos - pos1
                                if not is_indel:
                                    # Ensure rel_pos is valid after possible previous indels
                                    if rel_pos < len(r1_seq_list):
                                        # Ensure alt is different from ref
                                        if r1_seq_list[rel_pos] == v_val:
                                            r1_seq_list[rel_pos] = (
                                                "A" if v_val != "A" else "C"
                                            )
                                        else:
                                            r1_seq_list[rel_pos] = v_val
                                elif rel_pos > 10 and rel_pos < len(r1_seq_list) - 20:
                                    # Real Deletion: 5bp
                                    del_seq = (
                                        r1_seq_list[:rel_pos]
                                        + r1_seq_list[rel_pos + 5 :]
                                    )
                                    r1_seq_list = del_seq
                                    r1_cigar = (
                                        f"{rel_pos}M5D{len(r1_seq_list) - rel_pos}M"
                                    )

                        # Check R2
                        for v_pos, v_data in cv.items():
                            _v_ref, v_val, is_indel = v_data
                            if pos2 <= v_pos < pos2 + rl2:
                                rel_pos = v_pos - pos2
                                if not is_indel:
                                    if rel_pos < len(r2_seq_list):
                                        if r2_seq_list[rel_pos] == v_val:
                                            r2_seq_list[rel_pos] = (
                                                "A" if v_val != "A" else "C"
                                            )
                                        else:
                                            r2_seq_list[rel_pos] = v_val
                                elif rel_pos > 10 and rel_pos < len(r2_seq_list) - 20:
                                    del_seq = (
                                        r2_seq_list[:rel_pos]
                                        + r2_seq_list[rel_pos + 5 :]
                                    )
                                    r2_seq_list = del_seq
                                    r2_cigar = (
                                        f"{rel_pos}M5D{len(r2_seq_list) - rel_pos}M"
                                    )

                        r1_seq = "".join(r1_seq_list)
                        r2_seq = "".join(r2_seq_list)

                        # R1 (99 = paired, proper pair, mstrand, mate reverse)
                        reads.append(
                            (
                                pos1,
                                f"{read_id}\t99\t{name}\t{pos1}\t60\t{r1_cigar}\t=\t{pos2}\t{ins}\t"
                                + r1_seq
                                + "\t"
                                + "I" * len(r1_seq)
                                + "\tRG:Z:sample1\n",
                            )
                        )
                        # R2 (147 = paired, proper pair, reverse, mate mstrand)
                        reads.append(
                            (
                                pos2,
                                f"{read_id}\t147\t{name}\t{pos2}\t60\t{r2_cigar}\t=\t{pos1}\t-{ins}\t"
                                + r2_seq
                                + "\t"
                                + "I" * len(r2_seq)
                                + "\tRG:Z:sample1\n",
                            )
                        )

                    # Sort all reads by position
                    reads.sort()

                    # Write in batches
                    batch_size = 10000
                    for i in range(0, len(reads), batch_size):
                        f.write("".join([r[1] for r in reads[i : i + batch_size]]))

            # Convert SAM to BAM (already sorted)
            run_command(
                get_sam_view_cmd(threads="1", fmt="BAM", is_input_sam=True)
                + [sam_path, "-o", bam_path]
            )

        run_command(get_sam_index_cmd(bam_path))
        if os.path.exists(sam_path):
            os.remove(sam_path)
        logging.info(f"Created {bam_path} ({len(chroms)} chromosomes)")

    # 3. Create fake CRAM
    if "cram" in types:
        cram_path = os.path.join(outdir, "fake.cram")
        run_command(
            get_sam_view_cmd(threads="1", fmt="CRAM", reference=ref_path)
            + [bam_path, "-o", cram_path]
        )
        run_command(get_sam_index_cmd(cram_path))
        logging.info(f"Created {cram_path}")

    # 4. Create fake VCF with variants on all chroms
    if "vcf" in types:
        vcf_path = os.path.join(outdir, "fake.vcf")
        with open(vcf_path, "w") as f:
            f.write("##fileformat=VCFv4.2\n")
            f.write('##FILTER=<ID=PASS,Description="All filters passed">\n')
            # Use our chroms list directly for consistency
            for name, length in chroms.items():
                f.write(f"##contig=<ID={name},length={length}>\n")

            f.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n')
            f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1\n")

            # At 30x, density = 700. At 1x, density = 21000.
            if full_size:
                variant_density = max(700, int(21000 / max(1.0, coverage)))
            else:
                variant_density = 5000

            for name, length in chroms.items():
                num_variants = int(length / variant_density)
                if num_variants < 2:
                    num_variants = 2

                positions = sorted(
                    [random.randint(1, length) for _ in range(num_variants)]
                )

                batch_size = 50000
                for i in range(0, len(positions), batch_size):
                    batch = positions[i : i + batch_size]
                    lines = []
                    for pos in batch:
                        # Randomized REF/ALT using the seeded random generator
                        ref = random.choice(["A", "C", "G", "T"])
                        alt = random.choice(
                            [b for b in ["A", "C", "G", "T"] if b != ref]
                        )

                        lines.append(
                            f"{name}\t{pos}\t.\t{ref}\t{alt}\t100\tPASS\t.\tGT\t0/1\n"
                        )
                    f.write("".join(lines))

        vcf_gz = vcf_path + ".gz"
        with open(vcf_gz, "wb") as f_gz:
            run_command(["bgzip", "-c", vcf_path], stdout=f_gz)
        run_command(["tabix", "-p", "vcf", vcf_gz])
        os.remove(vcf_path)
        logging.info(f"Created {vcf_gz}")

    # 5. Create fake FASTQ (R1 and R2)
    if "fastq" in types:
        r1_path = os.path.join(outdir, "fake_R1.fastq.gz")
        r2_path = os.path.join(outdir, "fake_R2.fastq.gz")

        # We can use samtools fastq to generate these from the BAM we just made
        # -1 and -2 for paired end
        run_command(
            [
                "samtools",
                "fastq",
                "-1",
                r1_path,
                "-2",
                r2_path,
                "-0",
                "/dev/null",  # Ignore any singleton reads
                "-s",
                "/dev/null",  # Ignore any shared reads
                bam_path,
            ]
        )
        logging.info(f"Created {r1_path}")
        logging.info(f"Created {r2_path}")

    # Cleanup intermediate BAM if not requested
    if need_bam and "bam" not in types:
        if os.path.exists(bam_path):
            os.remove(bam_path)
        if os.path.exists(bam_path + ".bai"):
            os.remove(bam_path + ".bai")
        logging.debug(f"Removed intermediate BAM {bam_path}")
