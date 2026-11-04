# Table output: TSV or Parquet

Commands that write tables take `--format tsv|parquet`. Each one uses the
same table writer (`wgsextract_cli.core.table_writer`), so every table gets the
same columns, types and provenance in both formats.

| Command | Table | TSV (default) | With `--format parquet` |
| --- | --- | --- | --- |
| `vcf table` | One row per variant allele ([docs](variant-table.md)) | `<stem>_variants.tsv` | `<stem>_variants.parquet`, in place of the TSV. The SQLite database is unchanged. |
| `methylation pileup` | Per-modification summary | `<stem>.<context>.methylation.tsv` | `<stem>.<context>.methylation.parquet` |
| `methylation pileup` | Every site in modkit's bedMethyl | Not written: `.bedmethyl.gz` is the source | `<stem>.<context>.bedmethyl.parquet`, besides the bedMethyl |
| `info` (with `--format`, which implies `--detailed`), `info calculate-coverage`, `info coverage-sample` | Per-chromosome counts and breadth | Written only with `--format` | `<input>_chromosomes.parquet` |
| The same | QC stats, one row: the manifest's `info` metrics | Written only with `--format` | `<input>_qc.parquet` |
| `info calculate-coverage --bins` | Depth bands per contig | `<input>_coverage_bins.tsv` | `<input>_coverage_bins.parquet` |
| `microarray` | Every target's genotype, before liftover and vendor formatting | Written only with `--format` | `<stem>_genotypes.parquet` |

The vendor kits, VCF, BAM/CRAM and bedMethyl files keep their own formats,
because downstream tools need them exactly. `microarray --formats` still picks
the vendor kits; `--format` only adds the genotype table.

## What the Parquet files contain

- **Typed columns.** Text, 64-bit integers, doubles and booleans. A missing
  value is null, where the TSV leaves the field empty. Each column's
  description is in its field metadata.
- **zstd compression,** with column statistics, so engines can skip row groups
  by value.
- **Row groups of 131,072 rows,** written as the command streams. A
  whole-genome variant table or a bedMethyl with tens of millions of sites
  never sits in memory.
- **Rows in the order the command produces them:** the input's contig order,
  then position, for the variant table, the bedMethyl, and the chromosome and
  depth-band tables; the target list's order for microarray genotypes. Contig
  order is not lexicographic (`chr2` comes before `chr10`), so the order is
  recorded in the `sorted_by` metadata key instead of as Parquet sorting
  columns.
- **Provenance in the footer's key-value metadata:**

  | Key | Value |
  | --- | --- |
  | `wgsextract_version` | The version that wrote the file. |
  | `created` | UTC time, ISO 8601. |
  | `command` | The command, for example `vcf table`. |
  | `command_line` | The command line, with secrets redacted. From the Python API, `params` (the parameters as JSON) instead. |
  | `input`, `input_fingerprint` | The input's path and its `wgse-fp1:` fingerprint (the one the [result manifest](result-manifest.md) uses). |
  | `reference_build` | For example `hg38`, when it is known. |
  | `sorted_by` | The row order. |

  Commands add their own keys: `vcf table` adds the sample, the region and the
  annotations it found, and `info` says how breadth was measured.

Read the metadata with DuckDB or pyarrow:

```sh
duckdb -c "SELECT decode(key), decode(value)
           FROM parquet_kv_metadata('sample_variants.parquet')"
python -c "import pyarrow.parquet as pq; print(pq.read_schema('sample_variants.parquet').metadata)"
```

The TSV holds the same values as text. Reading the Parquet back and formatting
each value gives the TSV's rows, and the tests check this.

## Installing pyarrow

Parquet needs `pyarrow`, which WGS Extract does not install by default. A
command given `--format parquet` without it stops before doing any work, says
which Python lacks it, and suggests `--format tsv`. To add it:

- **Pixi (an `install.sh` install or a source checkout):** install the
  `parquet` environment with `pixi install --locked -e parquet` from the app
  directory (`wgsextract-cli/app/`) or the checkout, then run commands in it:
  `pixi run -e parquet wgsextract vcf table --vcf-input x.vcf.gz --format
  parquet`. The `dev` environment includes pyarrow too.
- **Your own Python environment:** `pip install pyarrow`, or install this
  package with its `parquet` extra (`pip install '.[parquet]'` from a
  checkout).

pyarrow publishes builds for Linux x86-64 and ARM64, macOS (Intel and Apple
silicon) and Windows x86-64, which covers every Pixi platform WGS Extract
supports. It has **no Windows ARM64 build** on PyPI or conda-forge, so on the
native Windows ARM64 Python being qualified in
[`packaging/windows-arm64`](../packaging/windows-arm64/README.md),
`--format parquet` fails with the error above. Use `--format tsv` there.
