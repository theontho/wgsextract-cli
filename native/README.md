# Apple Silicon aligner rebuilds

Optional macOS arm64 (macOS 13+) rebuilds of the two short-read aligners
WGS Extract uses, published as `macos-aligners-*` prereleases. The
`wgsextract` CLI does not use them yet: they are offered for testing until
they are qualified on full genomes (#364).

| Executable | Upstream | Notes |
| --- | --- | --- |
| `wgse-bwa` | BWA 0.7.19 | Native arm64 build. CPU by default (unmodified upstream code); `--metal` is **experimental**. Low memory: about 6 GB for a human index. |
| `wgse-bwa-mem2` | BWA-MEM2 2.3 | arm64 build through sse2neon. CPU by default; `--metal` is **experimental**. Needs much more memory than BWA (see below). |

Both write the same alignments as their upstream release, with or without
`--metal`; only the `@PG` version gains a `-wgse-<version>` suffix (and its
command line records any backend option). Each package has its own version,
printed by `--version`.

## Installing

The `macos-aligners-8` prerelease has `wgse-bwa` 0.7.0 and `wgse-bwa-mem2`
0.2.0. `wgse-bwa --metal` and `wgse-bwa capabilities` need `wgse-bwa` 0.2.0 or
later. Earlier prereleases: `macos-aligners-7` has `wgse-bwa` 0.7.0 and
`wgse-bwa-mem2` 0.1.0, whose `--metal` only extends seeds on the GPU;
`macos-aligners-6` has `wgse-bwa` 0.6.0, whose
CPU threads locate seeds and rescue mates one at a time;
`macos-aligners-5` has `wgse-bwa` 0.5.0, with
slower GPU kernels and no overlap of mapping and pairing;
`macos-aligners-4` has `wgse-bwa` 0.4.0, whose
`--metal` also rescues mates on the GPU but extends seeds on the CPU;
`macos-aligners-3` has `wgse-bwa` 0.3.0, whose
`--metal` seeds and locates on the GPU but rescues mates on the CPU;
`macos-aligners-2` has `wgse-bwa` 0.2.0, whose `--metal` only seeds on the
GPU; `macos-aligners-1` has `wgse-bwa` 0.1.0, which aligns on the CPU only.

Download into an empty directory with `gh` (or `curl`), which avoids the
browser quarantine flag that Gatekeeper applies to these ad-hoc-signed
executables, and check the archives against the SHA-256 digests GitHub
publishes for each release asset:

```bash
mkdir wgse-aligners && cd wgse-aligners
gh release download macos-aligners-8 -R WGSExtract/wgsextract-cli
gh release view macos-aligners-8 -R WGSExtract/wgsextract-cli --json assets \
  --jq '.assets[] | "\(.digest | ltrimstr("sha256:"))  \(.name)"' |
  shasum -a 256 -c
```

Continue only if every archive reports `OK`. Then extract them and put both
`bin` directories on `PATH` (add the `export` line to your shell profile to
keep it):

```bash
for archive in wgse-bwa-*-macos-arm64.tar.gz; do tar -xzf "$archive"; done
for dir in wgse-bwa-*-macos-arm64; do PATH="$PWD/$dir/bin:$PATH"; done
export PATH
wgse-bwa capabilities --json
wgse-bwa-mem2 capabilities --json
```

macOS kills a quarantined executable as soon as it starts. If an archive was
downloaded in a browser, clear the flag before running, for example
`xattr -dr com.apple.quarantine wgse-bwa-0.7.0-macos-arm64` (and
`wgse-bwa-mem2-0.2.0-macos-arm64`).

## Usage

```bash
wgse-bwa index hs38.fa
wgse-bwa mem -t 9 hs38.fa R1.fastq.gz R2.fastq.gz > out.sam
wgse-bwa --metal mem -t 9 hs38.fa R1.fastq.gz R2.fastq.gz > out.sam
wgse-bwa capabilities --json

wgse-bwa-mem2 index hs38.fa
wgse-bwa-mem2 mem -t 9 hs38.fa R1.fastq.gz R2.fastq.gz > out.sam
wgse-bwa-mem2 --metal mem -t 9 hs38.fa R1.fastq.gz R2.fastq.gz > out.sam
wgse-bwa-mem2 capabilities --json
```

Options before the command select the backend of `mem` (other commands ignore
them):

- default (`--backend cpu`): the upstream CPU code, unchanged;
- `--metal` (`--backend metal`): fails if Metal is unusable, and exits 1 if a
  GPU batch fails (the CPU still completes the alignments, so the output is
  complete and correct);
- `--backend auto`: like `--metal`, but falls back to the CPU in both cases
  with only a note on stderr.

What runs on the GPU:

- `wgse-bwa`: exact SMEM seeding (BWA's three-pass FM-index interval
  collection) and, from 0.3.0, suffix-array locate of the seeds (`bwt_sa`).
  Within each chunk of reads (`-K`) the GPU prepares batches of pairs from the
  front while the CPU threads extend the prepared pairs from the cached
  intervals and positions; when none is ready, a CPU thread maps an unprepared
  pair from the back of the chunk with upstream's code. On HG002 the GPU
  prepares about 88% of the pairs. Reads the GPU cannot seed exactly (over
  512 bp, too many intervals, or seeding options wider than 32 bits) are seeded
  by upstream's CPU code; on HG002 this is 0.3% of reads. From 0.4.0 the
  pairing step works the same way: the GPU aligns every mate-rescue window
  (`mem_matesw`'s `ksw_align2`) of batches of pairs from the front of the
  chunk, and the CPU threads use those results when they finish the pairs;
  on HG002 the GPU takes about 45% of the pairs. Windows with scores that need
  16 bits, mates over 256 bp, or bases other than A, C, G, T and N are
  aligned on the CPU. From 0.5.0 the GPU also extends seeds (`mem_chain2aln`'s
  `ksw_extend2`): after a CPU thread chains a pair, it stages each chain's
  first extension (left, then right from the left score) in a batch shared by
  all threads, the GPU extends full batches, and a CPU thread finishes the
  pair, using a GPU result only when the query, reference, start score, band
  and end bonus of upstream's call match it exactly (otherwise, as for the
  other seeds of a chain, upstream's code extends on the CPU). On HG002 the GPU
  extends the seeds of about 87% of the pairs, and 98% of those pairs'
  extension calls use its results. Extensions with queries over 256 bp or scores that
  need more than 16 bits are done on the CPU. From 0.6.0 the GPU kernels for
  mate rescue and seed extension are faster, and the pairing step of one chunk
  runs while the next chunk is mapped (the GPU is the bottleneck when pairing
  and the CPU threads when mapping), so up to three chunks of reads are in
  memory at once. While two chunks overlap, mapping and pairing each run `-t`
  threads, so up to twice `-t` alignment threads (plus a few GPU helper
  threads) can be runnable; lower `-t` to leave cores for other work. Seeding and locate also count bases in the index's
  occurrence blocks with fewer GPU instructions, and once a run has mapped 20M
  reads (on Macs with at least 16 GiB of memory) the GPU derives a suffix-array
  sample for every 16th row from the index's every-32nd-row samples, which
  halves the steps of each locate on the GPU and the CPU. From 0.7.0 the CPU
  threads do their share of the work faster, so they take more of each chunk
  from the GPU, which is the bottleneck: they locate a read's seeds with 16
  suffix-array walks interleaved, align a pair's mate-rescue windows together
  (running upstream's 8-bit kernel on up to four windows of the same length
  at once; windows that need the 16-bit kernel are left to upstream), sort the rescued alignments by pointer, and unpack reference bases
  with NEON, all with upstream's exact results. At most two GPU
  command buffers of the process run at once: with more, macOS sometimes
  aborts one to keep the display responsive (`ImpactingInteractivity`). It
  can still do so when other processes load the GPU; 0.6.0 then runs the
  aborted work again, up to four times, before it finishes on the CPU (and
  `--metal` exits 1).
- `wgse-bwa-mem2`: batches seed extension across worker threads, keeping small
  batches on the CPU. From 0.2.0 it also collects the SMEMs of batches of 16384
  reads on the GPU (upstream's three passes, reading the index's occurrence
  table in place), rescues mates on the GPU with `wgse-bwa`'s kernel, and pairs
  one chunk while it maps the next, as `wgse-bwa` 0.6.0 does. As in `wgse-bwa`,
  the GPU takes reads from the front of each chunk and the CPU threads from
  the back, the CPU uses a GPU result only for exactly the reads and inputs it
  was computed for, reads with more than 128 SMEMs or over 512 bases are
  seeded on the CPU, and at most two GPU command buffers run at once; aborted
  GPU work is retried and then finished on the CPU (`--metal` exits 1). 0.2.0
  also stops upstream's suffix-array locate from allocating a buffer sized by
  the seeds' total number of hits for every read, and lets it prefetch
  sampled entries, which it never did because of an operator-precedence slip;
  neither changes any result.

With a Metal backend both print GPU and CPU work counts to stderr.

## Memory

BWA-MEM2 trades memory for speed. Building its human index needs roughly
60–90 GB of RAM, and aligning needs the 15–20 GB index in memory. BWA fits
ordinary desktops. On hs38 with 150 bp HG002 pairs, `wgse-bwa` built the index
with a 6.2 GiB peak in 100 minutes, and aligning peaked at 5.3 GiB with 1 thread
and 5.9 GiB with 9 threads (default `-K`), or about 75 MiB per extra thread.
`wgse-bwa --metal` adds about 0.4 GiB, mostly the GPU's seed output buffer;
suffix-array locate shares the index's sampled suffix array with the GPU in
place and adds about 32 MiB. Its host memory does not grow with `-K` or `-c`:
at most three prepared batches wait for the CPU threads, and each locates at
most 2^21 rows (32 MiB); the rows of further reads are located on the CPU.
Mate rescue on the GPU (0.4.0) added about 0.13 GiB on HG002: at most three
prepared rescue batches wait for the CPU threads, each with at most 64 MiB of
mate and reference bases and as much GPU scratch, and further windows are
aligned on the CPU.
Seed extension on the GPU (0.5.0) added about 0.14 GiB on HG002: at most six
extension batches of 1024 pairs, each with up to 8 MiB of bases and 4 MiB of
jobs and results; when none is free, threads extend on the CPU.
0.6.0 pairs one chunk while it maps the next, which added about 0.15 GiB on
HG002 with `-K 10000000` (the reads and alignments of the extra chunk; more
with larger `-K`). After 20M reads, on Macs with at least 16 GiB, the denser
suffix-array sample adds one 33-bit entry for every 32 rows of the suffix
array, about 0.76 GiB for hs38 (built in about 18 s). 0.7.0 does not change
the peak.
With `--metal` the GPU uses the whole index
(4.3 GiB for hs38) in place, so it stays resident while aligning; it has not
been tested on 8 GB Macs.
`wgse-bwa-mem2` peaked at 16.3 GiB on hs38 with 9 threads and `-K 10000000`
(it loads its 17.0 GB of index files), so it does not fit in a 16 GB Mac.
`--metal` 0.1.0 added under 100 MB of GPU buffers; 0.2.0 peaked at 16.9 GiB.
It reads the index's 5.8 GiB occurrence table in place, and adds GPU seeding
scratch and output for one batch of 16384 reads (about 0.17 GiB for 150 bp
reads), up to three prepared seeding batches, mate-rescue buffers like
`wgse-bwa`'s, and the extra chunk of pairing one chunk while it maps the next
(about 0.16 GiB with `-K 10000000`; more with larger `-K`).

## Performance

Measured on an M1 Pro (8P+2E cores, 32 GB). All runs in each group produced
identical alignments.

Full genome (hs38, first 5M HG002 30x NovaSeq pairs, `-K 10000000`). Each row
is the mean of its runs. The 0.3.0 and 0.2.0 `--metal` rows come from one
session on AC power that interleaved them with the 0.3.0 CPU (two runs each,
three for 0.3.0 `--metal`):

| Aligner | Threads | Time | Peak RSS | vs CPU |
| --- | --- | --- | --- | --- |
| Bioconda bwa 0.7.19 | 9 | 528.5 s | 5.4 GiB | — |
| `wgse-bwa` 0.1.0 | 9 | 527.4 s | 5.4 GiB | — |
| `wgse-bwa` 0.3.0 | 9 | 482.7 s | 5.4 GiB | — |
| `wgse-bwa --metal` 0.2.0 (seeding) | 9 | 423.4 s | 5.8 GiB | −12.3% |
| `wgse-bwa --metal` 0.3.0 (seeding and locate) | 9 | 297.0 s | 5.8 GiB | −38.5% |
| `wgse-bwa --metal` 0.4.0 (also mate rescue) | 9 | 241.2 s | 5.9 GiB | −50.0% |
| `wgse-bwa --metal` 0.5.0 (also seed extension) | 9 | 213.1 s | 6.1 GiB | −55.9% |
| `wgse-bwa --metal` 0.6.0 (faster kernels, overlap) | 9 | 147.1 s | 6.2 GiB | −69.5% |
| `wgse-bwa --metal` 0.7.0 (faster CPU share) | 9 | 136.0 s | 6.2 GiB | −71.8% |

The 0.4.0 row comes from a later session on AC power that interleaved three
runs each of 0.4.0 `--metal` (243.6, 243.0 and 237.0 s), 0.3.0 `--metal`
(314.3, 304.5 and 310.3 s; mean 309.7 s) and the 0.4.0 CPU backend: 0.4.0
`--metal` took 22.1% less time than 0.3.0 `--metal`. Other processes loaded
the machine during two of the CPU runs (579.5 and 564.5 s); the third took
508.2 s. The 0.4.0 row's "vs CPU" therefore uses the earlier session's
482.7 s, in which 0.3.0 `--metal` was 4% faster than in this one.

The 0.5.0 row comes from a third session on AC power that interleaved three
runs each of 0.5.0 `--metal` (209.6, 214.3 and 215.5 s) and 0.4.0 `--metal`
(248.4, 233.4 and 232.5 s; mean 238.1 s), with the order reversed in the
third round, and two of the 0.5.0 CPU backend (548.1 s under background load
and 480.5 s): 0.5.0 `--metal` took 10.5% less time than 0.4.0 `--metal`, and
was faster in each round. It moves most of the gain into mapping (about
115 s, down from about 140 s); the pairing step is unchanged. Its "vs CPU"
uses 482.7 s like the rows above.

The 0.6.0 row comes from a fourth session on AC power that interleaved two
runs each of 0.6.0 `--metal` (146.8 and 147.3 s), 0.5.0 `--metal` (215.6 and
208.9 s; mean 212.3 s) and the 0.6.0 CPU backend (477.7 and 472.0 s), with
light background load (Spotlight and Photos indexing): 0.6.0 `--metal` took
30.7% less time than 0.5.0 `--metal` and 69.0% less than the CPU backend of
the same session. These 10M reads end before the denser suffix-array sample
is built; in runs that build it at the start, it saved a further 10–12% of
the time after the build.

The 0.7.0 row comes from a fifth session on AC power that interleaved three
runs each of 0.7.0 `--metal` (134.1, 133.6 and 140.3 s) and 0.6.0 `--metal`
(146.9, 148.2 and 153.8 s; mean 149.7 s), with the order reversed in the
third round, and two of the 0.7.0 CPU backend (533.6 and 537.9 s, slower than
in earlier sessions; the battery was charging): 0.7.0 `--metal` took 9.1% less
time than 0.6.0 `--metal`, and was faster in each round. Its CPU threads
mapped 2.4M of the 10M reads (1.6M in 0.6.0) and rescued the mates of 2.4M
pairs (1.7M). Its "vs CPU" uses 482.7 s like the rows above.

An earlier session measured 481.2 s (CPU) and 425.4 s (0.2.0 `--metal`). On
200k of those pairs 0.3.0 `--metal` saved 33% with 9 threads, 41% with 4 and
50% with 1, and 0.4.0 `--metal` took a further 21% less time than 0.3.0
`--metal` with 9 threads, and 0.5.0 `--metal` 11% less than 0.4.0 (11.1 s
against 12.4 s, three runs each).

BWA-MEM2's human index needs about 73 GB of memory to build, so it was built on
a Linux machine with upstream bwa-mem2 2.3 (the index files are the same on
macOS). On the same full-genome data and threads, `wgse-bwa-mem2` measured as
follows, in three interleaved rounds on AC power with the order reversed in
the third:

| Aligner | Threads | Time | Peak RSS | vs CPU |
| --- | --- | --- | --- | --- |
| `wgse-bwa-mem2` 0.1.0 | 9 | 447.5 s | 16.3 GiB | — |
| `wgse-bwa-mem2` 0.2.0 | 9 | 458.3 s | 16.3 GiB | — |
| `wgse-bwa-mem2 --metal` 0.1.0 (seed extension) | 9 | 395.7 s | 16.3 GiB | −11.6% |
| `wgse-bwa-mem2 --metal` 0.2.0 (also seeding, rescue, overlap) | 9 | 161.2 s | 16.9 GiB | −64.8% |

0.2.0 `--metal` (167.8, 172.2 and 143.5 s) took 59.3% less time than 0.1.0
`--metal` (440.8, 395.6 and 350.6 s) and was faster in each round; without
overlapping mapping and pairing it took 175.9 s. Each "vs CPU" compares with
the same version's CPU backend. The two CPU builds used the same user time
(2822–2848 s); their wall times varied with system time (144–367 s), which grew
when other applications' memory had to be compressed to make room for the
17 GB process on this 32 GB Mac. A profile of 0.2.0 `--metal` found mapping on
the critical path (162 s), with pairing overlapped under it; the GPU kernels
ran 45 s of seeding, 69 s of mate rescue and 31 s of seed extension.

Before the full index was available, BWA-MEM2 was measured with 10.6M read
pairs against a small reference (chr20 fragment, Y, MT) whose whole index fits
in cache:

| Aligner | Threads | Time | vs packaged build |
| --- | --- | --- | --- |
| Bioconda bwa 0.7.19 | 9 | 53.2 s | — |
| `wgse-bwa` | 9 | 51.9 s | −2.5% |
| Bioconda bwa-mem2 2.3 | 8 | 45.7 s | — |
| `wgse-bwa-mem2` | 9 | 35.0 s | −23.5% |
| `wgse-bwa-mem2 --metal` | 9 | 31.0 s | −32.2% |

On the CPU `wgse-bwa` matches the packaged build; its value is a
self-contained, dependency-free macOS build. Compiler tuning does not help:
Apple clang already targets `apple-m1`, and ThinLTO, full LTO, profile-guided
optimization and mimalloc each changed the 200k-pair time by about 1% or less,
and mimalloc changed the full-genome time by 0.4%.

On hs38 BWA's time splits roughly into SMEM collection (19%), suffix-array
locate (23%) and extension with mate rescue (45%). `--metal` 0.2.0 moved only
SMEM collection to the GPU, so it could save at most about a fifth of the run;
0.3.0 also locates the seeds there, for about 88% of the pairs. A profile of
0.3.0 `--metal` then put 52% of the CPU threads' busy time in mate rescue
(`ksw_align2`, 45% in its 8-bit kernel) and 30% in seed extension
(`ksw_extend2`). 0.4.0 aligns the rescue windows of about 45% of the pairs on
the GPU, which left seed extension (41% of the CPU threads' busy time) and the
remaining rescue on the CPU threads; 0.5.0 extends the seeds of most pairs on
the GPU. The GPU was then busy for most of both steps (its seeding, locate and
extension work in mapping, and mate rescue in pairing), but mapping was bound
by the CPU threads and pairing by the GPU. 0.6.0 therefore makes the rescue and
extension kernels faster and overlaps the two steps of consecutive chunks, and
borrows two ideas from BWA-MEM2 without its larger index: counting bases in a
whole occurrence block at once, and (on long runs) a denser suffix-array
sample. A profile of 0.6.0 `--metal` found the GPU busy for 94% of the run,
with its seeding and locate kernels bound by memory latency, while the CPU
threads spent 30% of their busy time in mate rescue's 8-bit kernel and 10% in
locate. 0.7.0 therefore speeds up the CPU threads' share, so that they take
more of the work from the GPU.

## Building

Requires Xcode command-line tools and CMake 3.24 or later. Upstream sources are
downloaded at pinned SHA-256 hashes.

```bash
cmake -S native -B out/native -DCMAKE_BUILD_TYPE=Release
cmake --build out/native -j
ctest --test-dir out/native --output-on-failure
cmake --install out/native --prefix out/native-prefix
```

`-DBUILD_TESTING=OFF` builds the release executables without tests or the
hidden GPU tuning flags. Metal tests are skipped when no Metal device is
available. `.github/workflows/macos_aligners.yml` builds, tests and packages
both executables, and publishes them when run with a `macos-aligners-N` tag.

## Licences

`wgse-bwa` combines BWA (GPL-3.0) with the code in `bwa/` and `metal-common/`
(GPL-3.0-or-later); its Metal seeding, locate, mate-rescue and extension kernels are
adapted from BWA's MIT-licensed `bwt.c`, `bwamem.c`, `bwamem_pair.c` and
`ksw.c` (`METAL-SEEDING-LICENSE`).
`wgse-bwa-mem2` combines BWA-MEM2 (MIT), sse2neon
(MIT) and safestringlib (MIT) with the code in `bwa-mem2/` (GPL-3.0-or-later,
like this repository). Each package ships its licences, including this
repository's GPL-3.0 text as `WGSE-LICENSE`, under `share/licenses/`, and the corresponding source is this directory plus the
pinned upstream archives in the CMake files.
