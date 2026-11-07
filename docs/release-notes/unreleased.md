<!-- Notes for the next release. PRs add user-facing entries here as they
land; the release PR renames this file to vX.Y.Z.md (see docs/releasing.md). -->

## New

- **The WGS Extract desktop app ships with the CLI:** its source moved into
  this repository (`gui/`), and each release now carries the app's installers
  for Linux, macOS and Windows next to the CLI it bundles. Installed apps
  update from here; the app's site moved to
  <https://wgsextract.github.io/wgsextract-cli/app/>
  ([#510](https://github.com/WGSExtract/wgsextract-cli/issues/510)).
- **Start from what you have:** the command reference
  ([docs/commands.md](../commands.md)) and the site's CLI page start with the
  commands to run, in order, for a BAM/CRAM, FASTQ, VCF or consumer raw-data
  file. Both are generated from the command metadata.
- **The desktop app reads each run's result manifest:** WGS Extract actions
  list the files they wrote with **Open** and **Show in Folder**, and show
  their warnings and key metrics, instead of leaving them in the log. A
  refused option is shown next to the action, and a missing tool offers
  **Open Setup**. The global `--result-file PATH` option, which writes the
  document to PATH while keeping the human-readable output, is now documented
  ([#529](https://github.com/WGSExtract/wgsextract-cli/issues/529)).
- `qc fastqc` fails if FastQC wrote no report, and warns (`no_reads`) when the
  input has no reads.

## Deprecated

These still work in this release. Each prints a warning (`deprecated_command`
in the result manifest) naming its replacement, and will be removed in 0.8.0
([#352](https://github.com/WGSExtract/wgsextract-cli/issues/352)):

- `wgsextract ref library-list`: use `wgsextract ref library --list`.
- Running VEP with bare `wgsextract vep --input ...`: use
  `wgsextract vep run --input ...`. After the removal, bare `vep` lists its
  subcommands, like `vcf`.
