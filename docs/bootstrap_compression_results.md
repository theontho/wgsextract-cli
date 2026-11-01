# Reference Bundle Compression Benchmark

Historical measurements used to choose the format of the reference bootstrap bundle. The benchmark script that produced them has since been removed from the repository.

For current pipeline benchmarks, use [`wgsextract benchmark`](../README.md#pipeline-benchmarks)
(`wgsextract benchmark --help` lists datasets and profiles). These historical
compression measurements are not extraction, alignment, or variant-calling benchmarks.

**Source data:** `reference/microarray/raw_file_templates`
**Raw size:** 335.76 MB

| Method | Level | Size | Ratio | Comp time (s) | Ext time (s) |
|:---|:---|:---|:---|:---|:---|
| 7z | 9 | 56.48 MB | 5.94x | 195.08 | 3.79 |
| 7z | 5 | 74.33 MB | 4.52x | 48.06 | 5.42 |
| xz | 9 | 72.58 MB | 4.63x | 180.85 | 2.95 |
| xz | 6 | 83.27 MB | 4.03x | 44.10 | 1.18 |
| zstd | 19 | 86.28 MB | 3.89x | 149.94 | 0.44 |
| bgzip | 9 | 91.33 MB | 3.68x | 33.50 | 0.54 |
| bzip2 | 1 | 99.27 MB | 3.38x | 16.18 | 6.02 |
| bzip2 | 9 | 104.16 MB | 3.22x | 17.01 | 6.82 |
| bgzip | 6 | 105.25 MB | 3.19x | 6.25 | 0.54 |
| 7z | 1 | 105.63 MB | 3.18x | 2.52 | 4.99 |
| xz | 3 | 107.20 MB | 3.13x | 25.45 | 0.75 |
| xz | 0 | 107.72 MB | 3.12x | 1.74 | 0.80 |
| gzip | 9 | 110.53 MB | 3.04x | 72.62 | 1.45 |
| zip | 9 | 110.53 MB | 3.04x | 75.55 | 2.13 |
| zstd | 15 | 110.52 MB | 3.04x | 56.73 | 0.45 |
| pigz | 9 | 110.54 MB | 3.04x | 10.29 | 0.58 |
| gzip | 6 | 111.53 MB | 3.01x | 14.66 | 1.44 |
| zip | 6 | 111.54 MB | 3.01x | 15.77 | 2.19 |
| pigz | 6 | 111.55 MB | 3.01x | 2.19 | 0.60 |
| zstd | 9 | 112.65 MB | 2.98x | 4.50 | 0.45 |
| zstd | 1 | 114.18 MB | 2.94x | 0.48 | 0.38 |
| bgzip | 1 | 119.71 MB | 2.80x | 1.47 | 0.53 |
| zstd | 3 | 123.34 MB | 2.72x | 0.88 | 0.49 |
| pigz | 1 | 127.87 MB | 2.63x | 0.43 | 0.54 |
| gzip | 1 | 127.88 MB | 2.63x | 2.82 | 1.59 |
| zip | 1 | 127.88 MB | 2.63x | 3.46 | 2.35 |
| lz4 | 9 | 148.91 MB | 2.25x | 2.25 | 0.30 |
| lz4 | 1 | 195.64 MB | 1.72x | 0.42 | 0.30 |

## Decision: `.tar.gz` of bgzipped files

7z at level 9 gave the best ratio, and a 7z archive of the uncompressed bundle was smaller still:

| Archive | Contents | Size |
| :--- | :--- | :--- |
| `wgsextract-reference-bootstrap.tar.gz` | bgzipped files | 167 MB |
| `wgsextract-reference-raw.7z` | uncompressed files | 92 MB |

The bootstrap bundle nevertheless ships as `wgsextract-reference-bootstrap.tar.gz` (see `BOOTSTRAP_URL` in `src/wgsextract_cli/core/constants.py`):

- VCFs, liftover chains, and other genomic files are already in the bgzipped `.gz` form the tools expect, so nothing needs recompressing after extraction.
- `tar` and `gzip` are available everywhere the CLI runs; 7z would be an extra dependency.
- The bundle changes rarely, so the larger download is a one-time cost.
