# Result manifests, `--json` and exit codes

Commands describe each run in a JSON document that follows the versioned schema
[`wgsextract.result/1`](../src/wgsextract_cli/assets/schemas/wgsextract.result.1.schema.json)
(JSON Schema draft 2020-12, shipped in the wheel). The document lists what the
command read and wrote, what it measured and warned about, and how it ran. Use
it instead of parsing log text or guessing output file names.

- **Manifest file.** A command that writes outputs also writes
  `<output-stem>.manifest.json` next to them, atomically. The stem comes from
  the first output file, minus a compression suffix and one extension.
  `sample_snps.vcf.gz` gives `sample_snps.manifest.json`, and
  `sample_mtDNA.bam` gives `sample_mtDNA.manifest.json`. Multi-output
  commands such as `microarray` choose a stem (`<sample>_microarray.manifest.json`).
  Report-only commands (`info`, `info chip`, `ref status`) write no file.
  A failed run that registered outputs still writes its manifest, with
  `status: "failed"`.
- **`--json`.** This global option prints the same document to stdout
  *instead of* the human-readable result, for every command. Logs, progress
  and anything external tools print go to stderr, so stdout holds exactly one
  JSON document, even when the run fails. Argument-parsing errors are the
  exception: argparse rejects them before the command starts, prints the usage
  text and exits 2 without a document.

## Document

| Field | Meaning |
| --- | --- |
| `schema` | Always `"wgsextract.result/1"`. |
| `command` | The subcommand path, e.g. `"vcf snp"` or `"lineage y-haplogroup"`. |
| `status`, `exit_code`, `error` | `ok`, `failed` or `cancelled`; the process exit code; and `null` or `{kind, message}`, where `kind` is one of `failure`, `usage`, `missing_dependency` or `cancelled`. |
| `inputs`, `outputs` | File records: `path` (absolute), `kind` (`bam`, `cram`, `vcf`, `fasta`, `fastq`, `tsv`, `json`, `directory`, ...), `role` (what the file is to this command, e.g. `input`, `snps`, `mt_bam`, `haplogroup_report`), `exists`, `size_bytes`, `mtime` (UTC ISO 8601), `fingerprint`, `index` (the `.bai`/`.crai`/`.csi`/`.tbi`/`.fai` beside it, if any) and per-file `metrics`. |
| `metrics` | Flat headline numbers and labels, e.g. `variants`, `haplogroup`, `sex`, `avg_depth`. |
| `warnings` | Structured warnings: `code`, `message` and scalar `details`. |
| `details` | The command-specific structured result, or `null`. |
| `manifest` | Absolute path of the manifest file this run wrote, or `null`. |
| `provenance` | `version` (package), `argv`, `params` (the parsed options; names containing token, secret, password, credential or api key are redacted), `tools` (versions of the external tools the command ran), `reference` (`path`, `build`, `fingerprint`), `platform`, `python`, `started`, `finished` and `elapsed_s`. |

Tool versions are probed only when a document is emitted, and only for tools the
run actually launched.

### Fingerprints

Inputs, outputs and the reference carry a `wgse-fp1:<sha256 hex>` fingerprint.
It is a SHA-256 of the file size plus the whole file when it is at most 3 MiB,
or of three 1 MiB blocks (head, middle, tail) when it is larger. That costs at
most 3 MiB of reads, even for a 100 GB BAM. Hashing the whole of such a file
would take minutes. The mtime is not hashed, so a copied file keeps its
fingerprint.

A fingerprint identifies a file cheaply ("is this the same BAM as last time?").
It is not an integrity check: an edit outside the sampled blocks that keeps the
size goes unnoticed. Use `wgsextract sign` for full-file digests.

### Warning codes

| Code | Raised by | Meaning |
| --- | --- | --- |
| `low_coverage` | `info --detailed`, variant calling, lineage, `microarray` | Depth is below a threshold (`depth`, `threshold`). |
| `sex_unknown` | `info --detailed` | The sex could not be inferred from X/Y coverage. |
| `long_reads` | `info --detailed` | Long-read data; the short-read tools give poor results. |
| `alt_unaware`, `duplicates_unmarked` | `info`, `vcf snp`/`indel`, `microarray` | The alignment has ALT contigs without ALT-aware mapping, or its duplicates are not marked. |
| `high_homozygosity`, `low_homozygosity`, `high_no_call_rate` | `microarray` | Genotype statistics outside the expected range. |
| `no_variants`, `no_reads`, `nothing_annotated` | variant calling, extraction, annotation | An output is empty. |
| `sample_female` | Y extraction | Female sample; Y outputs will be empty or noise. |
| `build_unknown`, `build_unsupported` | microarray, Y haplogroup | The reference build is unknown or unsupported by the tool. |
| `chip_ambiguous` | `info chip` | Two catalogued revisions match equally well. |
| `ystr_unverified_convention` | `lineage y-str` | Markers use a repeat-count convention the panel has not verified. |

New codes may be added in any release. Consumers should treat unknown codes
as generic warnings.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success. |
| 1 | Failure: bad input, a failed tool, an unexpected error. |
| 2 | Usage error: a missing or invalid option, or an argument-parsing error. |
| 3 | Missing dependency: a required external tool is absent, will not run, or is too old. |
| 128+N | Stopped by signal N (130 for Ctrl-C). |

## Versioning

Adding optional fields, warning codes, output roles, metrics or `details` keys is
backward compatible and keeps `wgsextract.result/1`. Removing or renaming a field,
or changing its meaning or type, requires `wgsextract.result/2` and a new schema
file. Check `schema` before reading a document.

## Compatibility

`info chip --json` and `ref status --json` existed before the manifest. Their
previous payloads are now the manifest's `details`: the `info chip` detection
(or `{"revisions": [...]}` with `--list`), and the `ref status` object. Read
`.details` where you used to read the top-level document. `ref status --values`
is unchanged.

## Coverage

These commands record full manifests: `info` (including `--detailed`),
`info chip`, `info calculate-coverage`, `info coverage-sample`, `ref status`, `vcf snp`, `vcf indel`,
`vcf annotate`, `vcf freebayes`, `vcf gatk`, every `extract` subcommand,
`lineage mt-haplogroup`, `lineage y-haplogroup`, `lineage y-str` and
`microarray`. Every other command honours `--json` and the exit codes, and
reports its status, inputs and provenance. Its outputs, metrics and warnings
will be added in later releases ([#345](https://github.com/theontho/wgsextract-cli/issues/345)).
