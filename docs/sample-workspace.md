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
still applies to them).

## What it records

```toml
schema = "wgsextract.sample/1"

[override]
sex = "Female"

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

- `[override]`: facts you set. They always win, and commands log that they used
  your override. `sex` (`Male`, `Female`) and `platform` (`illumina`, `ont`,
  `pacbio`, `ultima`, `dnbseq`) can be overridden. The build and depths are
  facts of each file, not of the sample. Use `--ref` or `--build` on a run
  instead.
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
wgsextract sample show --input ~/genomes/HG002/HG002.cram --json
```

`--workspace DIR` names the folder directly. Without it, `--genome` or the
`--input`'s folder is used. `sample show` lists the facts in effect for the
input, the facts recorded for each file (and whether that file has changed
since) and the results. `--json` prints the same data as the result's
`details`. The [typed API](api.md) has the same operations: `api.sample.show`,
`api.sample.set_overrides` and `api.sample.unset_overrides`.

If an override contradicts what the reads show, commands use the override and
`info --detailed` warns (`sex_override_differs`).

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
