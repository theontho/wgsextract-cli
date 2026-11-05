# Apple Silicon aligner rebuilds

Optional macOS arm64 (macOS 13+) rebuilds of the two short-read aligners
WGS Extract uses, published as `macos-aligners-*` prereleases. The
`wgsextract` CLI does not use them yet: they are offered for testing until
they are qualified on full genomes (#364).

| Executable | Upstream | Notes |
| --- | --- | --- |
| `wgse-bwa` | BWA 0.7.19 | Unmodified source, native arm64 build. Low memory: about 5 GB for a human index plus a little per thread. |
| `wgse-bwa-mem2` | BWA-MEM2 2.3 | arm64 build through sse2neon. CPU by default; `--metal` is **experimental**. Needs much more memory than BWA (see below). |

Both write the same alignments as their upstream release; only the `@PG`
version gains a `-wgse-<version>` suffix.

## Installing

Download into an empty directory with `gh` (or `curl`), which avoids the
browser quarantine flag that Gatekeeper applies to these ad-hoc-signed
executables, and check the archives against the SHA-256 digests GitHub
publishes for each release asset:

```bash
mkdir wgse-aligners && cd wgse-aligners
gh release download macos-aligners-1 -R WGSExtract/wgsextract-cli
gh release view macos-aligners-1 -R WGSExtract/wgsextract-cli --json assets \
  --jq '.assets[] | "\(.digest | ltrimstr("sha256:"))  \(.name)"' |
  shasum -a 256 -c
```

Continue only if every archive reports `OK`. Then extract them and put both
`bin` directories on `PATH` (add the `export` line to your shell profile to
keep it):

```bash
tar -xzf wgse-bwa-0.1.0-macos-arm64.tar.gz
tar -xzf wgse-bwa-mem2-0.1.0-macos-arm64.tar.gz
export PATH="$PWD/wgse-bwa-0.1.0-macos-arm64/bin:$PWD/wgse-bwa-mem2-0.1.0-macos-arm64/bin:$PATH"
wgse-bwa-mem2 capabilities --json
```

macOS kills a quarantined executable as soon as it starts. If an archive was
downloaded in a browser, clear the flag before running, for example
`xattr -dr com.apple.quarantine wgse-bwa-0.1.0-macos-arm64`.

## Usage

```bash
wgse-bwa index hs38.fa
wgse-bwa mem -t 8 hs38.fa R1.fastq.gz R2.fastq.gz > out.sam

wgse-bwa-mem2 index hs38.fa
wgse-bwa-mem2 mem -t 9 hs38.fa R1.fastq.gz R2.fastq.gz > out.sam
wgse-bwa-mem2 --metal mem -t 9 hs38.fa R1.fastq.gz R2.fastq.gz > out.sam
wgse-bwa-mem2 capabilities --json
```

Options before the command select the BWA-MEM2 backend:

- default (`--backend cpu`): BWA-MEM2's own SIMD code;
- `--metal` (`--backend metal`): batches seed extension across worker threads
  onto the GPU, keeping small batches on the CPU. Fails if Metal is unusable,
  and exits 1 if a GPU batch fails (the CPU still completes the alignments);
- `--backend auto`: like `--metal`, but falls back to the CPU in both cases.

With `--metal` the backend prints GPU and CPU batch counts to stderr.

## Memory

BWA-MEM2 trades memory for speed. Building its human index needs roughly
60–90 GB of RAM, and aligning needs the 15–20 GB index in memory. BWA fits
ordinary desktops. On hs38 with 150 bp HG002 pairs, `wgse-bwa` built the index
with a 6.2 GB peak in 100 minutes, and aligning peaked at 5.3 GB with 1 thread
and 5.9 GB with 9 threads (default `-K`), or about 75 MB per extra thread.
`--metal` adds under 100 MB of GPU buffers.

## Performance

Measured on an M1 Pro (8P+2E cores, 32 GB). All runs in each group produced
identical alignments.

Full genome (hs38, first 5M HG002 30x NovaSeq pairs, `-K 10000000`):

| Aligner | Threads | Time | Peak RSS |
| --- | --- | --- | --- |
| Bioconda bwa 0.7.19 | 9 | 528.5 s | 5.4 GB |
| `wgse-bwa` | 9 | 527.4 s | 5.4 GB |

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

`wgse-bwa` matches the packaged build at full-genome scale; its value is a
self-contained, dependency-free macOS build. On hs38 its time splits roughly
into SMEM collection (19%), suffix-array locate (23%) and extension with mate
rescue (45%). An exact GPU seeding prototype cut full-genome wall time by only
9%, so `wgse-bwa` has no Metal backend.

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

`wgse-bwa` is GPL-3.0 (BWA, plus this repository's build glue).
`wgse-bwa-mem2` combines BWA-MEM2 (MIT), sse2neon
(MIT) and safestringlib (MIT) with the code in `bwa-mem2/` (GPL-3.0-or-later,
like this repository). Each package ships its licences, including this
repository's GPL-3.0 text as `WGSE-LICENSE`, under `share/licenses/`, and the corresponding source is this directory plus the
pinned upstream archives in the CMake files.
