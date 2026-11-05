# Sample workspace (`wgsextract.sample.toml`)

Several commands need the same facts about a sample: its sex (Y-DNA, ploidy,
repeat typing), its sequencing platform (variant caller models, aligner
presets), the reference build and the read depth (genotype QC). Each used to
work them out again on every run. Counting chrX/chrY reads in a 30x CRAM, for
example, takes about a minute. A sample workspace records each fact once, with
where it came from, and lets you correct it.

The workspace is a genome folder: the folder of a `--genome` library entry, or
any folder holding `wgsextract.sample.toml` or `genome-config.toml`. Inputs in
other folders are not recorded (`info`'s own `<input>.wgse_info.json` cache
still applies to them), unless you set an override for one of them, which
creates the workspace there.

## What it records

```toml
schema = "wgsextract.sample/1"

[override]
sex = "Female"

[override."HG002.ont.bam"]
fingerprint = "wgse-fp1:41d7…"
platform = "ont"

[detected."HG002.cram"]
fingerprint = "wgse-fp1:9b1c…"

[detected."HG002.cram".sex]
value = "Male"
method = "chrY/chrX mapped-read ratio"
confidence = "high"
version = 2
by = "wgsextract 0.9.0 info"
at = "2026-11-04T22:18:27+00:00"

[detected."HG002.cram".build]
value = "hg38"
method = "alignment header contigs"
confidence = "high"
version = 1
by = "wgsextract 0.9.0 info"
at = "2026-11-04T22:18:27+00:00"

[outputs."HG002_snps.vcf.manifest.json"]
command = "vcf snp"
status = "ok"
at = "2026-11-04T22:31:02+00:00"
```

- `[override]`: facts you set, which win over detection (see
  [Overrides](#overrides)). `sex` (`Male`, `Female`) and `platform`
  (`illumina`, `ont`, `pacbio`, `ultima`, `dnbseq`) can be overridden. The
  build and depths are facts of each file, not of the sample. Use `--ref` or
  `--build` on a run instead.
- `[detected."<file>"]`: what commands detected from each file. The file is
  named relative to the workspace, or absolute if it lies elsewhere. Each fact
  has its value, the method that found it, a confidence (`high`, `medium`,
  `low`), the detector's version, the command and the time.
- `fingerprint`: the file's [content fingerprint](result-manifest.md) when its
  facts were detected. If the file is replaced or changed (for example by
  `realign` into the same name), its facts no longer match and are detected
  again on next use. A fact from an older detector version is also detected
  again.
- `[outputs]`: the [result manifests](result-manifest.md) that `--genome` runs
  wrote in the folder.

| Fact | Detected by | Used by |
| :--- | :--- | :--- |
| `sex` | `info --detailed`, or chrX/chrY/chr7 read counts on first use | `lineage y-haplogroup`, `extract ydna`, chrX/chrY ploidy in `vcf snp`/`indel`, `microarray` and `vep`, `typing str`, `vcf trio` (proband) |
| `platform` | read-group `PL` tag, else the first read name | `vcf snp`, `vcf deepvariant`, `vcf sv`, `methylation`, `align` |
| `build` | header contig MD5s or lengths | reference resolution for every command |
| `target_depths` | `info --detailed`, else a read count on first use | the low-depth warning of `vcf snp`/`indel`, `microarray` and `lineage` |
| `read_length`, `mean_coverage` | `info` | `target_depths`; shown by `sample show` |

## Commands

```sh
wgsextract sample show --genome HG002            # overrides, facts, results
wgsextract sample set --genome HG002 --sex Female
wgsextract sample unset --genome HG002 --sex
wgsextract sample set --input ~/Downloads/mom.bam --sex Female --platform ont
wgsextract sample show --input ~/genomes/HG002/HG002.cram --json
```

`--workspace DIR` names the folder directly. Without it, `--genome` or the
`--input`'s folder is used. `sample show` lists the facts in effect for the
input, the facts and overrides recorded for each file (and whether that file
has changed since) and the results. `--json` prints the same data as the result's
`details`. The [typed API](api.md) has the same operations: `api.sample.show`,
`api.sample.set_overrides` and `api.sample.unset_overrides`.

## Overrides

An override changes how commands work (chrX/chrY ploidy, keeping chrY, aligner
presets, the DeepVariant model), so it applies only where it is sure to be
right:

- **Sex, in a genome folder: folder-wide.** A genome folder (the `--genome`
  folder, a genome-library entry or a folder with `genome-config.toml`) holds
  one person's files, so `sample set --sex` there applies to all of them
  (`[override] sex = …`).
- **Sex elsewhere, and the platform always: one file.** Any other folder, such
  as Downloads, may hold several people's files, and one person's folder may
  hold reads from several platforms. These overrides belong to the `--input`
  file (`[override."<file>"]`) and are refused without one. They record the
  file's fingerprint and lapse, with a warning, when the file changes: set them
  again if they still hold. With `--genome`, the input defaults to the genome's
  alignment.
- A folder-wide `platform`, or a folder-wide `sex` outside a genome folder (for
  example written by hand), is ignored with a warning.

`sample unset` removes the folder-wide override and, with `--input`, that
file's own.

Commands still check an override against the reads: the recorded fact, else a
cheap detection (the read groups or first read name for the platform; chrX/chrY
read counts, once per file revision, for sex). When they disagree, the command
uses the override and warns `sex_override_differs` or
`platform_override_differs`, with the file, the detected value and the
override, in its [result manifest](result-manifest.md).

## Robustness

- Recording is best effort. A read-only folder never fails a command; the
  facts just aren't recorded.
- A file that isn't valid TOML, or that a newer release wrote
  (`wgsextract.sample/2`), is ignored with a warning and never overwritten.
  `sample set` refuses to change it.
- A file from before the schema existed keeps its overrides; its detected
  facts are dropped and detected again.
- Writes take a lock (`.wgsextract.sample.toml.guard`) and replace the file
  atomically, so concurrent commands on one sample don't lose facts.
- You may edit `[override]` by hand. wgsextract rewrites the file when it
  records facts, so comments are not kept. Invalid override values are
  reported and ignored.

The JSON Schema of the parsed document is
`src/wgsextract_cli/assets/schemas/wgsextract.sample.1.schema.json`.
