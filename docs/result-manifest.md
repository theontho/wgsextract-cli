# Result manifests, `--json` and exit codes

Commands describe each run in a JSON document that follows the versioned schema
[`wgsextract.result/1`](../src/wgsextract_cli/assets/schemas/wgsextract.result.1.schema.json)
(JSON Schema draft 2020-12, shipped in the wheel). The document lists what the
command read and wrote, what it measured and warned about, and how it ran. Use
it instead of parsing log text or guessing output file names.

- **Manifest file.** A command that writes outputs also writes
  `<output>.manifest.json` next to them, atomically. The name is the first
  output file's name with any compression suffix (`.gz`, `.bgz`, `.zst`,
  `.bz2`, `.xz`) removed. The type extension stays, so outputs that differ only
  in type keep separate manifests. `sample_MT.vcf.gz` gives
  `sample_MT.vcf.manifest.json`, `sample_MT.fasta` gives
  `sample_MT.fasta.manifest.json`, and `sample_mtDNA.bam` gives
  `sample_mtDNA.bam.manifest.json`. Multi-output commands such as `microarray`
  name their manifest after the run (`<sample>_microarray.manifest.json`;
  `<vcf>_<sample>_microarray.manifest.json`, with a `sample` metric, for a
  sample chosen from a multi-sample VCF). `vcf trio` always writes
  `trio.manifest.json`, whichever modes ran.
  Report-only commands (`info`, `info chip`, `ref status`) write no file,
  except `info --format`, whose manifest is named after the first table
  (`<input>_chromosomes.parquet.manifest.json`).
  A failed run that registered outputs still writes its manifest, with
  `status: "failed"`. A manifest never replaces this run's `--log-file`; when
  the two would share a name, the manifest is skipped with a warning.
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
| `manifest` | Absolute path of the manifest file this run wrote, or `null` (including when writing it failed). |
| `provenance` | `version` (package), `argv` and `params` (the command line and the parsed options as the command used them, after `--genome` selection; values of options whose names contain token, secret, password, credential or api key are redacted, and URLs there, and in error and warning messages, lose their user name, password, query string and fragment), `tools` (versions of the external tools the command ran), `reference` (`path`, `build`, `fingerprint`), `platform`, `python`, `started`, `finished` and `elapsed_s`. |

Tool versions are probed only when a document is emitted, and only for tools the
run actually launched.

### Fingerprints

Inputs, outputs and the reference carry a `wgse-fp1:<sha256 hex>` fingerprint.
It is a SHA-256 of the file size plus the whole file when it is at most 3 MiB,
or of three 1 MiB blocks (head, middle, tail) when it is larger. That costs at
most 3 MiB of reads, even for a 100 GB BAM. Hashing the whole of such a file
would take minutes. The mtime is not hashed, so a copied file keeps its
fingerprint. Only regular files are read. A named pipe, `/dev/stdin` or a process
substitution is recorded with `size_bytes` and `fingerprint` set to `null` and
is never opened, so the command still gets its whole stream.

A fingerprint identifies a file cheaply ("is this the same BAM as last time?").
It is not an integrity check: an edit outside the sampled blocks that keeps the
size goes unnoticed. Use `wgsextract sign` for full-file digests.

### Warning codes

| Code | Raised by | Meaning |
| --- | --- | --- |
| `low_coverage` | `info --detailed`, variant calling, lineage, `microarray`, `methylation` | Depth is below a threshold (`depth`, `threshold`). |
| `sex_unknown` | `info --detailed`, `typing repeats` | The sex could not be inferred from X/Y coverage. |
| `long_reads` | `info --detailed` | Long-read data; the short-read tools give poor results. |
| `short_read_n50` | `info` | Long reads' N50 is under 3 kb, which suggests degraded DNA or a failed library (`read_n50`, `exact`, `threshold`). |
| `low_mapped_read_fraction` | `info --detailed` | Under 95% of long reads map: contamination, a failed library or the wrong reference (`mapped_read_pct`, `threshold`). |
| `mapped_read_fraction_unavailable` | `info --detailed` | Long reads' mapped percentage could not be estimated: no read starts in the sampled windows (a small targeted file). |
| `alt_unaware`, `duplicates_unmarked` | `info`, `vcf snp`/`indel`, `microarray` | The alignment has ALT contigs without ALT-aware mapping, or its duplicates are not marked. |
| `high_homozygosity`, `low_homozygosity`, `high_no_call_rate` | `microarray` | Genotype statistics outside the expected range. |
| `alignment_memory` | `align`, `realign` | minimap2 is estimated to need more RAM than is available (`aligner`, `estimated_gib`, `available_gib`, and `suggested_threads`/`suggested_memory` that fit, or `null`). The run continues. |
| `no_variants`, `no_reads`, `nothing_annotated` | variant calling, alignment, extraction, annotation | An output is empty. |
| `low_confidence_findings` | `vcf clinvar`, `vcf gnomad --max-af`, `vcf pharmcat`, `analyze` | Some reported calls are low confidence (`count`, `total`, `output`, top `reasons`); see [variant-confidence.md](variant-confidence.md). |
| `unconfirmed_clinical_findings` | `vcf clinvar`, `analyze` | The sample carries ClinVar pathogenic/likely pathogenic calls (`count`): a technical screen to confirm with an accredited laboratory, not a diagnosis. |
| `confidence_first_sample_only` | confidence labelling | A multi-sample VCF was judged by its first sample (`sample`). |
| `sample_female` | Y extraction | Female sample; Y outputs will be empty or noise. |
| `build_unknown`, `build_unsupported` | microarray, Y haplogroup | The reference build is unknown or unsupported by the tool. |
| `chip_ambiguous` | `info chip` | Two catalogued revisions match equally well. |
| `gaps_not_excluded` | `vcf filter --exclude-near-gaps` | The reference or its gap table was not found, so variants near gaps were kept. |
| `gvcf_reference_missing`, `unconfident_parent_genotypes` | `vcf trio` | A gVCF trio has no `--ref`, so reference blocks count only where they start at a variant; de novo calls rest on a parent's low-quality reference call (`count`, `total`). |
| `no_indel_scores` | `vcf cadd` | The CADD indel file is missing, so indels are not scored. |
| `contigs_skipped` | `vcf to-fasta` | VCF contigs absent from the reference were left out (`count`). |
| `annotation_skipped`, `annotation_failed` | `vcf chain-annotate`, `analyze` | One annotation was skipped (`annotation`, `reason`) or failed. |
| `read_groups_mixed`, `read_group_conflict` | `bam unalign`, `bam merge` | Read groups differ in a way that loses or merges information. |
| `few_modification_tags`, `opposite_strand_modifications`, `pacbio_5hmc_limited`, `unknown_platform` | `methylation` | The input's MM/ML modification tags are sparse, strand-mixed, or from a platform with limited calls. |
| `genotype_ambiguous`, `high_copy_number`, `no_call` | `typing` | A star-allele or HLA call is ambiguous, has extra copies, or failed (`gene`). |
| `repeat_outside_normal_range` | `typing repeats` | A repeat length is outside its normal range (`locus`). |
| `output_exists`, `input_failed` | `vep` | An input was skipped because its output exists (`path`), or failed while the batch went on. |
| `vep_info_missing`, `chromosomes_missing` | `vep verify` | The VEP cache lacks its `info.txt` or chromosome directories (`chromosomes`). |
| `dict_missing` | `ref verify` | The reference has no sequence dictionary. |
| `step_failed` | `benchmark` | A benchmark step failed (`step`, `log`). |
| `cache_path_too_long` | `microarray` on Windows | The reference library path (`path`, `length`) is too deep for its derived caches to fit the 260-character `MAX_PATH`, so the run is uncached; keep the library path at most `limit` characters. |
| `unsigned` | `sign verify` | The checksum manifest has no signature; only hashes were checked. |
| `ystr_unverified_convention` | `lineage y-str` | Markers use a repeat-count convention the panel has not verified. |

New codes may be added in any release. Consumers should treat unknown codes
as generic warnings.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success. |
| 1 | Failure: bad input, a failed tool, an unexpected error. |
| 2 | Usage error: a missing or invalid option, or an argument-parsing error. |
| 3 | Missing dependency: a required external tool is absent, will not run, or is too old, including when a command reports it as part of its own failure message. |
| 128+N | Stopped by signal N (130 for Ctrl-C). |

How refusals are classified:

- **Your option choice is unsupported → 2.** Conflicting flags (`vcf sv
  --pacbio --ont`, `align --r2` with ONT, Ultima or pbmm2 reads, `vcf gatk
  --scatter --gpu`, a `--batch-size` that is not positive), aligner, preset
  and platform combinations that `align` and `realign` refuse (`--preset` with
  BWA, BWA for long reads, pbmm2 for ONT, `--gpu` with ONT, Ultima,
  `bwa-mem2`, PacBio CLR/subreads or a minimap2 preset Parabricks lacks, pbmm2
  with a temporary directory), `vcf deepvariant --gpu` with `--checkpoint` or
  a model Parabricks lacks, conflicting DeepVariant model selectors, a Clair3
  `--model` for another platform, a malformed `vcf sv --region`, `bam merge`
  with fewer than two distinct inputs or an `--output` extension that
  contradicts `--cram`, `bam to-ubam` without exactly one of `--input` and
  `--r1`, `methylation --combine-strands` with a non-CpG context, `sign verify
  --allowed-signers` without `--identity`, a non-positive `benchmark
  --coverage` or `--target-count`, `qc fake-data --legacy-bam --full-size`,
  `example-genome download --method aspera` without a key, and options
  this platform cannot honour while the command itself can run: `--gpu` off
  Linux x86-64, and `--aligner pbmm2` on Windows. Choosing different options
  fixes the run.
- **The command or input is unsupported here → 1.** A command with no native
  build on this platform (for example `vep`, `typing`, `vcf deepvariant` or
  `vcf sv` on Windows), or an input that needs one (Ultima reads needing
  GATK, or `vcf deepvariant --gpu` on reads it detects as Ultima). No choice
  of options would help.
- **A required tool is missing → 3.** This includes `deps check` when a
  mandatory tool is missing, `deps check --tool NAME` when `NAME` is not
  found, and a missing container setup (Docker or its daemon, an image, or for
  `--gpu` the NVIDIA driver or container runtime, or `curl`/`ascp` for
  `example-genome download`). Missing optional tools are reported but keep exit 0.

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
`info chip`, `info calculate-coverage`, `info coverage-sample`, `ref status`,
`align`, `realign`, every `vcf` subcommand, every `extract` subcommand,
`lineage mt-haplogroup`, `lineage y-haplogroup`, `lineage y-str`,
`microarray`, every `bam`, `qc`, `ref`, `vep` and `typing` subcommand,
`analyze`, `pet`, `methylation`, `repair`, `benchmark`, `example-genome
download`, `sign` and `report`.

`align` and `realign` record the `aligner` and the alignment's `records`; for a
BAM also `mapped_records` and `unmapped_records` (from the index, without
decoding reads). CRAM counts come from container headers, which also count
unmapped reads placed beside their mates, so a CRAM reports `records` only.
Variant callers and annotators record `variants` per output VCF; filtered
outputs (`--min-score`, `--max-af`) are separate outputs with their own counts.

Other commands' metrics:

- `bam` subcommands that write a BAM/CRAM record its `records` as `align` does
  (an unsorted or unaligned BAM has no index to count from, so it has none);
  `bam merge` adds `merged_inputs`, `bam unalign` its `read_groups`.
- `qc fastp` records read counts before and after filtering, the Q30 rate and
  duplication rate; `qc fastqc` the module pass/warn/fail counts and total
  sequences; `qc vcf` the `bcftools stats` totals (records, SNPs, indels,
  ts/tv).
- `analyze` reads each step's result document (the steps still print their
  tables and results) and includes the step's manifest (role
  `step_manifest`), its outputs with their metrics and its warnings with their
  details (tagged with `step`), even for steps that write no manifest such as
  `info --detailed`; plus `sex` and `significant_variants`.
- `methylation` records sites, median coverage and weighted percent modified
  per modification (and, with `--format parquet`, the `rows` of the
  `bedmethyl_table`); `typing` the called genotypes (`cyp2d6_genotype`, one
  diplotype per HLA gene, flagged repeats); `repair` the records read and
  repaired.
- `vep` records `inputs`, `succeeded`, `skipped` and `failed`, and `variants`
  per annotated VCF; a directory of inputs writes `vep.manifest.json`.
- Reference installers (`ref library --install`, `ref bootstrap`, the
  annotation downloads, `ref gene-map`) record every file they add or change in
  the library; `ref index` its new index files; `ref count-ns` the N totals.
- `benchmark` records its report files and step counts in
  `benchmark.manifest.json` in the run directory.
- `report` records its documents, the source manifests it read (role
  `manifest`), the number of `findings` (and `findings_<label>` per
  confidence label) and each section's status (`section_<name>`); its
  `details` is the `wgsextract.report/1` document
  ([personal-report.md](personal-report.md)).

`repair` in pipe mode (no `--input`) streams to stdout, so `--json` is a usage
error there. Commands that only print (`deps`, `info` listings, `ref library
--list`, `config`) record no outputs.

## MultiQC

`qc multiqc-metrics` turns a directory of manifests (and saved `--json`
output) into MultiQC custom-content tables, and `qc multiqc` includes them in
its report. See [Working with professional pipelines](interop.md#multiqc).
