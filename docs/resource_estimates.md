# CLI resource preflight and reusable estimates

Native `align` (BWA/BWA-MEM2), `realign`, `bam sort`, `bam to-bam`,
`bam to-cram`, and `vcf snp`/`indel`/`gatk` report estimates before starting
their heavy pipelines. For example:

```text
Preflight align: estimated peak / available (GiB)
Preflight memory OK      18.13 / 24.00 GiB
Preflight disk   LOW     93.86 / 80.00 GiB at /data/results
```

Each filesystem gets one disk row. Temporary files, output, output indexes,
extracted FASTQs when needed, and missing reference indexes are **added together
when they share a volume**, not separately compared with the same free space.
GATK's system temporary directory can be on a different volume. A nonexistent
output directory is checked against its nearest existing ancestor, without
creating it. Available RAM uses the existing native psutil API; disk space uses
Python's native `disk_usage`, including Windows drives.

`LOW` warns. Existing refusal policies are unchanged: explicitly selected
BWA-MEM2 refuses inadequate RAM, BWA warns, auto can fall back to BWA, and
realignment/whole-file sorting retain their existing disk guards. `--no-preflight`
still bypasses realignment's disk refusal, not its memory guard; informational
estimates are still printed. Realignment reports before FASTQ extraction and
alignment reports again when the final aligner is selected.

Eligible paired BWA/bwa-mem2 realignment without `--keep-fastq` streams newly
extracted reads (forced refresh of cached FASTQs at the exact output paths
retains file handling): `samtools view | collate | fastq` writes interleaved
pairs to stdout and the aligner reads them with `-p`, through ordinary process
pipes on every platform, including native Windows. There is no
`extracted_fastqs` demand. minimap2, GPU and long-read workflows keep files.

Streaming uses file extraction's collate bucket count (20× BAM / 70× CRAM input
size against threads × per-thread memory, 64–200 buckets). Collate's output
order depends on that count, so the aligner receives the same pairs in the same
order either way and the realigned records are identical. A 30x genome streams
whenever one bucket fits beside alignment in RAM.

Collate holds one bucket in memory while BWA maps. Its estimate is the largest
bucket's records (the indexed input count over the buckets, plus six standard
deviations) times the power-of-two buffer of the largest BAM record in the
read-length sample, plus 128 bytes of per-record bookkeeping and 64 MiB of
headroom. Without an indexed count it falls back to 30× BAM / 100× CRAM of the
input size per bucket. This amount is added to mapping's RAM preflight; if it
does not fit, realignment extracts files without changing the selected aligner.
It is an estimate, not an enforced RSS limit:

| Input | Records | Buckets | Measured collate RSS per held record | Estimate per record |
| --- | ---: | ---: | ---: | ---: |
| HG00096 chr20 BAM (1000 Genomes 30x, 852 MB) | 17,275,064 | 16 / 64 / 133 / 200 | 677 / 690 / 709 / 725 bytes | 1,152 bytes |
| HG002 chr20 BAM (GIAB NovaSeq 35x, 1.09 GB) | 19,467,155 | 16 / 64 / 133 / 200 | 684 / 716 / 758 / 795 bytes | 1,152 bytes |

Measured per-record figures are samtools 1.24 collate's maximum RSS over the
largest bucket's records, so fixed process memory inflates them at high bucket
counts.

Collate writes every bucket to temporary files before it emits a read, then
deletes each bucket as it drains while the sorter's spills grow. The disk
guard therefore takes the larger of collation's spill and sort spill plus
output, the same as file extraction without the FASTQs between them; the
resource report counts the sort `temporary` estimate once. With a configured
temporary directory on another volume, that volume needs the larger of the two
spills and the output volume holds the result. If streaming spills do not fit
but file extraction does, realignment uses files.
The alignment sample reads at most 4096 input SAM records / 16 MiB for the mean
paired-primary read length and the largest record of any kind; it does not
replace the indexed input count.

On 2 Mb chr20 slices (BWA, 2 threads, 512M, one core) the streamed BAM had the
same ordered records and header as file extraction except the BWA `@PG`
command line (`-p … -`); file extraction stayed byte-identical. Streaming wrote
38% less (HG00096 113 → 75 MiB, HG002 316 → 196 MiB) at the same wall time and
within 3% of the same peak RSS.

Preflight is advisory: `preflight_resources()` guards estimation and reporting
against `ValueError`, `OSError` and subprocess failures, warns that the resource
estimate is unavailable, and lets the command continue. Existing dependency,
memory and disk guards still enforce their own policies normally.

These are conservative estimates, **not reservations, memory limits or
guarantees**. When size or free-space measurement is unavailable the row says
`UNKNOWN`, never a misleading `OK`. Regional disk estimates deliberately retain
whole-input upper bounds rather than guessing coverage. CPU models do not
describe GPU/Parabricks or long-read pipelines.

## GUI/API integration

There is deliberately no second command-execution/dry-run path. A GUI can call
the same read-only function used by the CLI:

```python
import json
from wgsextract_cli.core.resource_estimates import estimate_resources

estimate = estimate_resources(
    "align",
    input_paths=["sample_R1.fq.gz", "sample_R2.fq.gz"],
    output_dir="results",
    reference="hs38DH.fa.gz",
    aligner="bwa",
    threads="6",
    memory="256M",
    reads=679_639_896,  # individual reads, not pairs
    output_format="CRAM",
)
print(json.dumps(estimate.to_dict()))
```

The function only inspects file metadata, reference index sizes/FAI, RAM and
free disk. It neither creates output directories nor executes alignment/indexing
tools. The caller supplies resolved reference/aligner choices; the CLI still
validates dependencies normally. A GUI may supply `temp_dir`, and `realign`
callers set `extract_fastq=True` only for newly extracted reads. Existing input
files and reused FASTQs are not charged again.

`to_dict()` returns `schema_version: 1`, `operation`, `notes`, and `resources`.
Each resource has `resource` (`memory` or `disk`), `estimated_bytes`,
`available_bytes`, `status`, `components` (byte counts) and `paths`.
Unavailable sizes are JSON `null`. `report_resources(estimate)` is the optional
logging presentation layer.

The GUI/API estimator remains strict: invalid settings raise `ValueError` so
clients can validate them. CLI callers use
`preflight_resources(lambda: estimate_resources(...))` instead.

For alignments, callers can obtain `reads` using the existing bounded
`core.read_counts.fastq_input_reads` or indexed
`core.warnings.indexed_read_count`. Omitting it leaves the D3 model's explicit
680-million-read RAM fallback; disk uses input bytes and format (12 bytes/read
for compact CRAM, 80 for BAM, 70 for FASTQ). A missing reference uses the human
reference RAM fallback, and adds a structured `UNKNOWN` disk row with no guessed
filesystem for reference-index storage.

## Calibration and limitations

- Ordinary 150-bp, up-to-six-thread alignment RAM retains the #179/#194 model:
  resident reference indexes, aligner working set, read-scaled samblaster and
  transient overhead, and threads × sort memory plus 25%.
- BWA-MEM working memory also accounts for platform, sampled mean read length
  and threads. FASTQ count and mean length come from the same bounded prefix;
  BAM/CRAM preflight does not decode the input to measure read length.
  Unknown lengths use 150 bp for ordinary short reads and 300 bp for Ultima.
  GUI/API callers can supply `platform="ultima"`, `read_length=300` and
  `samblaster=False` for single-end Ultima inputs.
- At fixed `-K 100000000`, Ultima allows 4.5 GiB of working memory at two
  threads and 10 GiB at four threads (300 bp), **in addition to** the index,
  sort and pipeline overhead. It scales with read length and populated batch,
  with a 2 GiB floor. This conservatively covers HG008/hs38DH: BWA peaked at
  9.18 GiB at two threads; four-thread anonymous memory reached 14.70 GiB
  before the 16 GiB unit OOM. The two-thread diagnostic timed out: this is a
  memory observation, not completed full-genome correctness. For 313,736,048
  reads and 256M sort memory, estimates are approximately 12.0 GiB at two
  threads and 18.1 GiB at four. Longer ordinary reads and more than six BWA
  threads also increase the allowance. BWA-MEM2 calibration is unchanged.
- Available RAM honors Linux cgroup-v2 ancestor caps, including reclaimable
  inactive file cache; Windows/macOS retain native available-RAM reporting.
  Namespace/subtree mounts retain their visible descendant limits.
  LOW-memory BWA warnings recommend a lower thread count plus the resolved
  `--memory` value, keeping the modeled per-sort-thread allowance fixed.
  **No automatic thread cap is applied:** requested threads and BWA arguments
  remain unchanged. Existing BWA advisory and explicit BWA-MEM2 refusal
  policies remain. Read complexity, flow-tag volume and tool versions can
  change peaks; estimates are not allocation guarantees.
- Phase-2 H2 processed **679,639,896 reads** (339,819,948 pairs). Its compressed
  FASTQ inputs totalled **47.84 GB**, and its binned-quality output was only
  **8.63 GB CRAM**, plus a 1.14 MB CRAI. Do not extrapolate that unusually good
  compression to full-quality inputs.
- The conservative disk envelope uses **120 bytes/read sort spill**,
  **28 bytes/read CRAM**, **100 bytes/read BAM**, and **75 bytes/read extracted
  FASTQ**. At H2 depth this is **81.56 GB temporary sort space and 19.03 GB
  CRAM** (75.96 and 17.72 GiB), within the phase-2 planning ranges of 70–90 GB
  sort space and about 16–20 GB CRAM. Output indexes add 1% (at least 64 KiB).
  Existing reference indexes cost no new disk; missing ones receive an
  additional two-resident-index-size build allowance on the reference volume.
- Ultima pipelines using GATK flow duplicate marking pass
  `duplicate_marking="gatk-flow"`. Their temporary budget includes retained
  sorted/marked BAMs: up to two BAMs (200 bytes/read) for CRAM output, rather
  than assuming only sort spill and final CRAM coexist.
- VCF output uses 10% of input bytes, with another output-sized allowance for
  per-contig files/concatenation (twice for GATK). Regional GATK CRAM conversion
  also budgets a whole-input BAM upper bound on the output volume.
  GATK scattering additionally charges retained shard VCFs and their indexes to
  the **output volume**, where they coexist with the gathered output; JVM
  temporary storage remains a separate demand on the system temporary volume.
- Sort RAM is threads × `--memory` plus 25% and 256 MiB overhead; conversion
  allows 64 MiB per codec thread plus 256 MiB overhead. bcftools allows 1 GiB per
  active contig worker plus 256 MiB. Serial calling is the default; `--parallel`
  (`parallel=True` in the API) enables multiple workers, capped by reference
  contig count and one worker for an explicit region. GATK uses a **6 GiB
  heuristic per worker**, not a claim about its Java heap limit. The opt-in
  `vcf gatk --scatter` flow-mode path selects at most `--threads` workers and
  one worker per 6 GiB of currently available physical RAM, then supplies that
  count as `gatk_workers` to the reusable estimator. Each parallel JVM has an
  explicit 4 GiB heap and one native PairHMM/GC worker; the remaining allowance
  covers non-heap memory. Unknown RAM or room for only one worker retains the
  original unscattered command. This is a conservative concurrency policy, not
  a reservation or hard RSS cap. These non-alignment models
  are conservative heuristics, not measured guarantees for every caller, read
  length or depth.

Compression, read lengths, depth, extra flow-specific stages and tool versions
can change these costs. Missing reference-index construction has its own
existing tool-specific guards; the alignment RAM row describes mapping, not
index construction. No swap/pagefile is counted as available RAM.
