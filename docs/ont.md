# Oxford Nanopore

`align` and `realign` detect ONT from `@RG PL:ONT` or UUID read names in
BAM/CRAM/FASTQ (including gzipped FASTQ). Read groups take precedence; mixed
platforms are not assigned one platform. An explicit `--platform` overrides
detection. Unknown input still defaults to Illumina. SRA-renamed FASTQs without
platform metadata need `--platform ont`.
Only ONT gains new automatic alignment defaults. Revio/PacBio-, Ultima- and
MGI-named FASTQs, and `PL:PACBIO`/`PL:DNBSEQ` BAMs, retain the previous Illumina
alignment defaults unless `--platform` is explicit. The pre-existing
BAM/CRAM `PL:ULTIMA` automatic path is unchanged. Other detected platforms
may be reported, but reporting does not select their alignment workflows.

CPU ONT alignment uses minimap2 `map-ont`, carries `MM`, `ML` and `MN` through
`samtools fastq -T MM,ML,MN` and `minimap2 -y`, and soft-clips supplementary
alignments (`-Y`) so the modification coordinates still describe the full read.
Sorting retains these tags.
FASTQ input first passes through `samtools import -T MM,ML,MN`, so ordinary
ONT `runid`/channel comments do not become invalid SAM fields with `-y`.
Realignment streams single-end tagged FASTQ instead
of reusing potentially untagged paired FASTQs. `--keep-fastq` and GPU ONT
alignment are refused rather than silently losing modification data; for a
retained intermediate use `samtools fastq -T MM,ML,MN` separately.

For CRAM realignment, `--input-ref` supplies the original decoding FASTA for
both platform detection and extraction; `--ref` selects the alignment target.
Without `--input-ref`, a CRAM lacking platform read groups resolves its
original decoding FASTA from the reference library and sequence-dictionary
signature **before** its read names are probed. A failed CRAM read-name probe
is an error, never a silent fallback to paired short-read extraction.
The usual realignment disk-space preflight also applies to streamed ONT reads.

## Small variants

`vcf deepvariant` automatically selects `ONT_R104` for detected ONT input.
`--ont` is an alias for `--model-type ONT_R104`; explicit model selectors
override detection. Contradictory ONT selectors are rejected.

```sh
wgsextract vcf deepvariant -i hg002-ont.bam --ref chr20.fa \
  --region chr20:10000000-10010000 --threads 1 --outdir ont-variants
```

This model supports **R10.4.1 only**. R9.4.1/R9.4 data is unsupported; the CLI
warns when `@PG` or `@RG DS` basecaller metadata identifies it. UUID names
cannot identify chemistry, so absence of a warning does not establish support.
Use Google's installed `google/deepvariant:1.10.0` image (the default), or an
installed `run_deepvariant` that provides this model. Dependencies are checked,
never installed automatically.

`ONT_R104` requires x86-64. Linux ARM64 remains WGS-only, and ONT calling is
also refused on Apple Silicon rather than assuming emulation is qualified.
Other models retain their existing platform policy. The pinned Parabricks
4.8.0-1 supports `deepvariant --mode ont`, selected by `--gpu`; this command
mapping is tested, but GPU ONT accuracy has not been hardware-qualified here.
CPU container temporary files and HOME stay inside the owned output work
directory and are removed after the run.

### Clair3 (optional)

`vcf clair3` runs [Clair3](https://github.com/HKU-BAL/Clair3) 2.x, an
ONT-trained small-variant caller. It is optional, and DeepVariant remains the
default ONT caller. Clair3 is qualified on **Linux x86-64 only**. It is not
available on Windows (use WSL2), and Bioconda's macOS and ARM builds are
refused because they have not been validated here. Install it into its own
Pixi environment with `pixi install -e clair3`. The CLI checks for it but
never installs it.

```sh
wgsextract vcf clair3 -i hg002-ont.bam --ref chr20.fa \
  --region chr20:10000000-11000000 --threads 2 --outdir ont-clair3 \
  --model r1041_e82_400bps_sup_v500
```

The model must match the basecaller that produced the reads. It is chosen in
this order:

1. `--model NAME|DIR`;
2. `clair3_model` in `config.toml`, for example
   `clair3_model = "/data/clair3-models/r1041_e82_400bps_sup_v420"`;
3. the Dorado/Guppy basecaller model named in the `@RG` (`DS:basecall_model=`,
   ID) or `@PG` header lines. For example, `dna_r10.4.1_e8.2_400bps_sup@v5.0.0`
   selects `r1041_e82_400bps_sup_v500`, R10.4 E8.1 selects `r104_e81_*_g5015`,
   and R9.4.1 selects `r941_prom_sup_g5014`.

If the header names no basecaller (common after realignment, or for older
public data), or names several, the command stops and asks for `--model`. A
model for another pore, kit or speed than the metadata states is refused. A
different accuracy (hac/sup) or basecaller version is allowed with a warning.
HiFi and Illumina models are refused, as are `*_with_mv` models, which need
Dorado move tables and `--enable_dwell_time`.

A `NAME` is one of the models Bioconda bundles in the environment
(`.pixi/envs/clair3/bin/models`; the error lists them). Download others
(for example Dorado v4.2/v4.3 models) from HKU's PyTorch catalogs, keeping
the directory name:

```sh
m=r1041_e82_400bps_sup_v420
mkdir -p clair3-models/$m
for f in pileup.pt full_alignment.pt; do
  curl -fL -o clair3-models/$m/$f \
    https://www.bio8.cs.hku.hk/clair3/clair3_models_rerio_pytorch/$m/$f
done
```

Then set `clair3_model` in `config.toml` to that directory, or pass it with
`--model`.

Bundled models are in `clair3_models_pytorch/`, Rerio-derived models in
`clair3_models_rerio_pytorch/`. Clair3 1.x TensorFlow models do not work with
Clair3 2.x. Before calling, the CLI checks both files against the SHA-256
digests pinned in `src/wgsextract_cli/assets/clair3/models.json`, which
covers 31 ONT models from both catalogs. A mismatch is an error. A model
without a pin, such as a custom one, runs with a warning.

On a 1 Mb slice (chr20:10-11 Mb) of public HG002 R10.4.1 sup reads (ONT's
GIAB 2023.05 release, which records `basecaller_cfg
dna_r10.4.1_e8.2_400bps_sup@v4.0.0`), realigned with `realign` and scored with
`rtg vcfeval` against GIAB v4.2.1 inside its confident regions (1,355 SNVs and
234 indels):

| Caller | SNV F1 | Indel F1 | Overall F1 |
| --- | --- | --- | --- |
| `vcf deepvariant` (`ONT_R104`) | 0.999 | 0.875 | 0.982 |
| `vcf clair3 --model r1041_e82_400bps_sup_v400` (matching model) | 0.997 | 0.862 | 0.979 |
| `vcf clair3 --model r1041_e82_400bps_sup_v410` (bundled) | 0.998 | 0.891 | 0.984 |
| `vcf clair3 --model r1041_e82_400bps_sup_v420` | 0.998 | 0.839 | 0.977 |

The callers are within a few indels of each other on this slice, so
DeepVariant stays the default. This is a regional check, not whole-genome
qualification (#299).

## Structural variants

`vcf sv` runs Sniffles2 for detected ONT input; `--ont` requests the same
explicitly, and the platform-neutral `--long-read` selects Sniffles for any
long-read data. pbsv is PacBio-only, so it is refused for ONT (detected or
`--ont`), whether chosen by `--caller pbsv` or by `--pacbio`'s pbsv default. `--ont`/`--long-read` with `--caller delly`, and `--ont`
with `--pacbio`, are rejected; an explicit `--caller delly` on detected ONT runs
but warns that Delly is a short-read caller. PacBio behavior is unchanged:
`--pacbio` still prefers pbsv (falling back to Sniffles) with CCS thresholds,
and PacBio input without flags still uses Delly.

```sh
wgsextract vcf sv -i hg002-ont.bam --ref chr20.fa \
  --region chr20:10000000-11000000 --threads 1 --outdir ont-svs
```

Sniffles2 runs with its defaults, which are tuned for both ONT and HiFi. Pass
a tandem-repeat BED with `--tandem-repeats` (now forwarded to Sniffles as well
as pbsv) to improve calls in repeats. Mosaic mode (`--mosaic`) is not used:
this command calls germline SVs. Install Sniffles (for example via the `pacbio`
Pixi environment); it is checked, not installed automatically.

Platform detection, tagged CPU alignment/realignment, regional x86-64
DeepVariant ONT_R104 calling, optional Linux x86-64 Clair3 calling, and Sniffles
SV calling are supported.
Whole-genome ONT accuracy and methylation analysis are not qualified by this
change; whole-genome validation is tracked in #299.

## CLI / GUI contract

No new option is needed for detection. Leave `--platform` unset for automatic
selection; explicitly sending `--platform illumina` disables it. The CLI logs
the detected platform. `info` recognizes `PL:ONT` even when read names were
renamed. BAM/CRAM variant callers share the same cached, file-revision-aware
platform resolver; detection alone does not qualify short-read callers for ONT.
GUI integrations should allow automatic platform selection and surface the
tag-preservation limitations above.
Expose `ONT_R104` and `--ont` for DeepVariant, preserve an unset model for
automatic selection, and show its chemistry and architecture restrictions.
For `vcf clair3`, expose `--model` (leave it unset for config or metadata
selection) and hide the command on Windows. For `vcf sv`, expose `--ont`/`--long-read`, leave `--caller` unset for
automatic Sniffles selection, and do not offer pbsv for ONT input.
The GUI repository is separate; these CLI changes do not update it.
