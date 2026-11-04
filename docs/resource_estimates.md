# CLI resource preflight and reusable estimates

Native `align` (BWA/BWA-MEM2), `realign`, `bam sort`, `bam to-bam`,
`bam to-cram`, and `vcf snp`/`indel`/`gatk`/`deepvariant` (CPU, every platform)
report estimates before starting their heavy pipelines. For example:

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

Eligible paired BWA realignment without `--keep-fastq` streams newly extracted reads
(forced refresh of cached FASTQs at the exact output paths retains file handling):
there is no retained `extracted_fastqs` demand. The **full 1 GiB overlap allowance**
is included in mapping's RAM preflight even when its bucket estimate is smaller,
and `collation_spill` is conservatively added
alongside sorter spills on the configured temporary volume (or output volume
when none is configured). Collate removes buckets as they drain, so this disk
sum is an upper bound, not a prediction of actual peak. If streaming spills
do not fit but file extraction does, realignment uses files.
The alignment read-length sample reads at most 4096 input SAM records / 16 MiB
and applies paired-primary filtering within that prefix; it does not replace
the indexed input count.

Streaming collation's **30× BAM / 100× CRAM** expansion model is heuristic, not an
enforced native-process RSS limit. Estimated bucket memory plus 64 MiB bookkeeping must
fit both the configured collation budget and the 1 GiB overlap ceiling. The
200-bucket ceiling gives best-case input limits around **6.25 GiB BAM / 1.875 GiB CRAM**;
smaller budgets or less available RAM reduce eligibility. 30x WGS uses files.
Mapping must fit with the full overlap allowance before streaming starts; if it
does not, extraction uses files without changing the baseline aligner. Ordinary
bucket-estimation overruns consume accounted headroom; no hard RSS guarantee
is claimed. File extraction retains its original 20×/70× bucket policy.

Calibration uses the worst observed decoded-SAM/compressed-input expansion or
projected record allocation below, adds **50% margin**, then rounds upward to
the next multiple of ten:

| Input | Decoded expansion | Record-allocation estimate |
| --- | ---: | ---: |
| Actual tiny paired BAM (302 records, 8,642 bytes) | 8.66× | 13.42× |
| Same tiny CRAM (1,789 bytes) | 44.53× | 64.83× |
| Chr20 BAM (961,538,643 bytes, 17,651,438 primary reads) | 8.19× | 11.75× |
| Nominal30x HG00096 CRAM (15,741,370,187 bytes, 768,049,130 primary reads) | 23.17× | 31.23× |
| Same WGS count projected onto a representative 60 GiB BAM | 5.66× | 7.63× |

These are **not isolated native collate RSS measurements**. Tiny allocation
estimates use actual QNAME/CIGAR/SEQ/auxiliary shapes, power-of-two payload
allocation and 128 bytes per record for structure/sort/allocator bookkeeping.
WGS projections use preserved reference-header/input-count contracts and an
assumed conservative Illumina shape: 151 bases, 64-byte QNAME, eight CIGAR
operations, 128 auxiliary bytes, rounded to a 512-byte payload plus the same
128-byte overhead. The 151-base length and 60 GiB BAM size are assumptions,
not header measurements. Verified decoded chr20/WGS outputs are shape proxies,
not independent scans of all input SAM records. Thus the worst ratios,
13.42×/64.83×, become 30×/100× after margin and rounding; pathological
compression or name skew can still exceed these estimates.

Preflight is advisory: `preflight_resources()` guards estimation and reporting
against `ValueError`, `OSError` and subprocess failures, warns that the resource
estimate is unavailable, and lets the command continue. Existing dependency,
memory and disk guards still enforce their own policies normally.

These are conservative estimates, **not reservations, memory limits or
guarantees**. When size or free-space measurement is unavailable the row says
`UNKNOWN`, never a misleading `OK`. Regional disk estimates deliberately retain
whole-input upper bounds rather than guessing coverage. CPU models do not
describe GPU/Parabricks or long-read pipelines; see
[hardware and acceleration](hardware.md) for GPU alignment.

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

### DeepVariant postprocess_variants

DeepVariant's stages run one after another, so its RAM estimate is the largest
of TensorFlow inference (`2 + 14 × batch_size/1024` GiB on x64, 30 on ARM),
`make_examples` shards (1 GiB each on x64, 2 on ARM) and `postprocess_variants`,
plus 1 GiB of driver headroom.

`postprocess_variants` splits the calling regions into partitions (DeepVariant
1.9+ `--num_partitions`, at least `--cpus`) and sorts each partition's
call_variants records in memory, one partition per worker process. The
estimator replays DeepVariant's own `partition_calling_regions` on the
reference `.fai` and the `--region` selection, then charges each worker for its
partition's bases:

- 768 MiB per worker, plus
- 4.75 records per kilobase × 2 KiB per record.

Calibration (#173, HG008 Ultima ~30x on hs38DH, `--threads 5`): DeepVariant's
five partitions held 2.93, 3.01, 3.10, 3.30 and 1.06 million call_variants
records (13.4 million in total; 4.70 per kb at most, in chr13–chr20). Three
workers were OOM-killed mid-sort under a 12 GB cap at 3.39, 4.49 and 6.14 GB
anonymous RSS, which are lower bounds on their peaks. The model gives 7.1 GiB
per worker, about 36 GiB for the five, so the whole genome at `--threads 5`
needs 37 GiB rather than the 17 GiB that inference alone suggests. Density
was measured on one 30x Ultima genome; deeper or noisier inputs and other
models can produce more candidates. A chr20 replay with 16 shards peaked at
0.6 GiB per worker, consistent with the base allowance.

Before running, `vcf deepvariant` (CPU, not WES, which DeepVariant
postprocesses in one sequential pass) bounds this step by the RAM available
now, capped by `docker info`'s engine memory, such as Docker Desktop's VM:

1. If `--threads` workers fit, the command is unchanged.
2. Otherwise it keeps every worker and passes
   `--postprocess_variants_extra_args num_partitions=N` with the fewest
   partitions that make the largest one fit, logged at INFO. More partitions
   cost runtime: each worker rereads all call_variants outputs.
3. If no partition count up to 128 fits, it also lowers
   `--postprocess_cpus`, with a warning.

`num_partitions` needs DeepVariant 1.9 or later. It is used with the pinned
default images, image tags of version 1.9 or later, and other images or a
local `run_deepvariant` whose `postprocess_variants --helpfull` lists it (asked
only when more partitions are wanted). Otherwise step 2 is skipped: only
`--postprocess_cpus` is lowered, with a warning. That saves little: fewer
workers sort bigger partitions, so a whole genome still needs about 30 GiB.
When even that cannot fit, the refusal suggests DeepVariant 1.9+.
4. If one worker cannot fit, it refuses before `make_examples` starts and
   gives the memory needed and available.

The plan uses memory available at the start; other jobs that start later can
still take it. In a container, an OOM kill of any process (from a Docker memory
cap or a host out of memory) is reported by `docker events`. The command then
kills the container and fails with an actionable error instead of letting
DeepVariant's worker pool wait forever on its lost worker. A local
`run_deepvariant` on `PATH` has no such watcher. On any failure, complete
call_variants outputs (and the small model's `make_examples` outputs, if any)
are moved to a new `deepvariant_intermediates/<date-time>-<n>/` directory in
the output directory instead of being deleted with the work directory, so
outputs of separate failed runs are never mixed.

Compression, read lengths, depth, extra flow-specific stages and tool versions
can change these costs. Missing reference-index construction has its own
existing tool-specific guards; the alignment RAM row describes mapping, not
index construction. No swap/pagefile is counted as available RAM.
