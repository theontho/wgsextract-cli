# Resuming long workflows

A whole-genome `realign`, `vcf deepvariant` or `analyze comprehensive` runs
for hours as a series of steps. Each records its completion, so a run stopped
by a crash, Ctrl-C, an out-of-memory kill or a reboot resumes when you rerun
the same command. Steps that finished are skipped, and the run restarts at
the first step that didn't.

| Command | Steps | Journal |
|---|---|---|
| `realign` | `extract` (BAM/CRAM to FASTQ), `align` (map, mark duplicates, sort), `index` | `<output>.steps.json`, e.g. `sample_aligned.bam.steps.json` |
| `vcf deepvariant` | `stage_reads` (CRAM 3.1 inputs only), `make_examples`, `call_variants`, `postprocess_variants` | `deepvariant.vcf.gz.steps.json` |
| `analyze comprehensive` | `info`, `mt-haplogroup`, `y-haplogroup`, `snp`, `indel`, then `annotate:<vcf>` and `discovery:<vcf>` for each VCF | `analyze.steps.json` in the output folder (each batch sample's own folder) |

## When a step is skipped

When a step finishes, the journal (JSON, schema `wgsextract.steps/1`) records:

- a key made from its settings, the versions of the tools it ran and the
  version of the step itself
- the fingerprint of each input, and the run of the step that made it
- the size and [content fingerprint](result-manifest.md) of each output

A rerun skips the step only if all of the following hold:

- the key is unchanged
- every input is unchanged, and no earlier step was rebuilt
- every output is still the one the step wrote

Otherwise the step runs again, and so does every step that uses its outputs.
The log gives the reason for each step, for example:

```text
Skipping step extract: complete (2026-11-05T02:16:41+00:00).
Running step align: an earlier run was interrupted during it.
Running step align: settings changed: sample None -> 'Bea'.
Running step align: x_aligned.bam changed size (1048576 -> 1048476 bytes; truncated or rewritten).
```

Settings that don't change a step's output, such as threads and memory, are
left out of its key. You can therefore resume with a different `--threads`.

A step's inputs include the data it reads, not just the sample: the
reference FASTA, region BED files, DeepVariant checkpoint files and
installed annotation data (a new ClinVar or gnomAD release reruns
`annotate`). Tool versions include Haplogrep for `mt-haplogroup` and, for
DeepVariant in Docker, the image's ID, so replacing an image under the same
tag reruns every stage.

## Partial outputs

A step is marked `running` in the journal before it starts. It is marked
`complete` only after all of its outputs are written. Outputs are written
under temporary names and renamed into place when they are complete. A run
killed mid-step therefore never leaves a step, or a file, that looks done.
The rerun removes the half-written temporary files and redoes the step.

DeepVariant is the exception that keeps partial work. Until the VCF is
final, its stages work in `.deepvariant.work` in the output folder. A
`make_examples` run that was interrupted keeps its finished shards: GNU
parallel's `--joblog` and `--resume-failed` rerun only the shards that hadn't
finished. Those shards are kept only when the rerun has the same key and
inputs. Otherwise the folder is cleared. A resume with a different
`--threads` keeps the earlier run's shard count, and `--threads` sets only
how many shards run at once. Once the VCF is written, the work folder is
deleted.

## Options

Each of the three commands takes these options:

| Option | Effect |
|---|---|
| `--force` (alias `--no-resume`) | Rerun every step, ignoring the journal. |
| `--force-step STEP` | Rerun `STEP` and the steps that use its outputs. Repeatable. For `analyze comprehensive`, `annotate` and `discovery` force the step for every VCF. |
| `--dry-run` | Print each step, whether it would run or be skipped, and why. Runs nothing. |

```console
$ wgsextract realign --input sample_hg19.bam --ref hs38.fa --dry-run
realign plan (sample_hg19_aligned.bam.steps.json):
  extract  skip  complete (2026-11-05T02:16:41+00:00)
  align    run   an earlier run was interrupted during it
  index    run   step align runs before it
```

The plan is also in the run's [result manifest](result-manifest.md) as
`details.steps`, along with the metrics `steps_run` and `steps_skipped`.
Library users see each decision as a `step` progress event (see the
[API](api.md)).

## One run at a time

A run holds a lock on its journal (`.<journal>.lock`) until it ends. A second
run into the same output fails at once and names the lock, instead of both
writing one file. The operating system releases the lock when a run dies, even
from SIGKILL.

## Limits

- Resuming works within a step's boundaries. The rest of an interrupted
  `align` (one pipeline from FASTQ to sorted, duplicate-marked BAM) is redone
  from its start. Only DeepVariant's `make_examples` shards resume within a
  step.
- `realign` keeps its extracted FASTQs only until the alignment is complete.
  When they are temporary, they are deleted after the alignment, and the
  journal notes that. A later `--force-step align` therefore extracts them
  again.
- When `wgsextract` itself is SIGKILLed during a DeepVariant stage in Docker,
  the container may keep running, because `docker run` doesn't forward
  SIGKILL. Stop it with `docker ps` and `docker stop` before rerunning. A
  Ctrl-C (SIGINT) or SIGTERM stops it.
- The GPU (`--gpu`) DeepVariant path runs as one step, so it doesn't take
  `--dry-run` or `--force-step`.
