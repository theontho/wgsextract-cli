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

The `macos-aligners-2` prerelease has `wgse-bwa` 0.2.0 and `wgse-bwa-mem2`
0.1.0. `wgse-bwa --metal` and `wgse-bwa capabilities` need `wgse-bwa` 0.2.0 or
later; `macos-aligners-1` has `wgse-bwa` 0.1.0, which aligns on the CPU only.
`wgse-bwa` 0.3.0, which also locates seeds on the GPU, is built from this
directory and will be in the next `macos-aligners-*` prerelease.

Download into an empty directory with `gh` (or `curl`), which avoids the
browser quarantine flag that Gatekeeper applies to these ad-hoc-signed
executables, and check the archives against the SHA-256 digests GitHub
publishes for each release asset:

```bash
mkdir wgse-aligners && cd wgse-aligners
gh release download macos-aligners-2 -R WGSExtract/wgsextract-cli
gh release view macos-aligners-2 -R WGSExtract/wgsextract-cli --json assets \
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
`xattr -dr com.apple.quarantine wgse-bwa-0.2.0-macos-arm64` (and
`wgse-bwa-mem2-0.1.0-macos-arm64`).

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
  by upstream's CPU code; on HG002 this is 0.3% of reads.
- `wgse-bwa-mem2`: batches seed extension across worker threads, keeping small
  batches on the CPU.

With a Metal backend both print GPU and CPU work counts to stderr.

## Memory

BWA-MEM2 trades memory for speed. Building its human index needs roughly
60–90 GB of RAM, and aligning needs the 15–20 GB index in memory. BWA fits
ordinary desktops. On hs38 with 150 bp HG002 pairs, `wgse-bwa` built the index
with a 6.2 GiB peak in 100 minutes, and aligning peaked at 5.3 GiB with 1 thread
and 5.9 GiB with 9 threads (default `-K`), or about 75 MiB per extra thread.
`wgse-bwa --metal` adds about 0.4 GiB, mostly the GPU's seed output buffer;
suffix-array locate shares the index's sampled suffix array with the GPU in
place and adds about 32 MiB. With `--metal` the GPU uses the whole index
(4.3 GiB for hs38) in place, so it stays resident while aligning; it has not
been tested on 8 GB Macs.
`wgse-bwa-mem2 --metal` adds under 100 MB of GPU buffers.

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

An earlier session measured 481.2 s (CPU) and 425.4 s (0.2.0 `--metal`). On
200k of those pairs 0.3.0 `--metal` saved 33% with 9 threads, 41% with 4 and
50% with 1.

The BWA-MEM2 human index cannot be built on this machine, so BWA-MEM2 was
measured with 10.6M read pairs against a small reference (chr20 fragment, Y,
MT) whose whole index fits in cache. Full-genome gains will be smaller:

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
0.3.0 also locates the seeds there, for about 88% of the pairs, which leaves
mostly extension on the CPU threads.

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
(GPL-3.0-or-later); its Metal seeding and locate kernels are adapted from BWA's
MIT-licensed `bwt.c` and `bwamem.c` (`METAL-SEEDING-LICENSE`).
`wgse-bwa-mem2` combines BWA-MEM2 (MIT), sse2neon
(MIT) and safestringlib (MIT) with the code in `bwa-mem2/` (GPL-3.0-or-later,
like this repository). Each package ships its licences, including this
repository's GPL-3.0 text as `WGSE-LICENSE`, under `share/licenses/`, and the corresponding source is this directory plus the
pinned upstream archives in the CMake files.
