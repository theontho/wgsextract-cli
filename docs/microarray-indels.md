# Sequence-defined microarray insertion/deletion probes

Indel calling is **opt-in**. Supply the actual manufacturer manifest applicable
to each selected chip/export layout, rather than inferring DNA alleles from a
person's `II`, `DD` or `DI` call:

```sh
wgsextract microarray --input sample.bam --ref reference \
  --formats 23andme_v5_2025,ancestry_v2_2024 \
  --indel-manifest 23andme_v5_2025=exact-chip-build37.csv \
  --indel-manifest ancestry_v2_2024=another-exact-chip-build37.csv.zip \
  --region chr20 --outdir out/indels
```

An uncompressed, gzip-compressed or single-CSV ZIP Illumina manifest is accepted.
It must provide `Name`, `SNP`, `GenomeBuild`, `Chr`, `MapInfo` and `SourceSeq`.
Only GRCh37, sequence-defined `[I/D]`/`[D/I]` probes are eligible. The manifest
must identify the same marker **and** coordinate in the selected vendor template.
A generic manufacturer's array is not a guarantee of a vendor's customized chip
revision. Bind manifests separately to format IDs; conflicting definitions are
quarantined, including unsupported duplicate definitions, not resolved by
first/last row order.

## Calling and output

- Both complete flanks and the actual insertion/deletion sequence must match
  exactly one reference haplotype. Both orientations are considered, but a strand
  label alone is never enough to infer an allele. Ambiguous/unmatched reference
  contexts stay missing. GRCh38 uses a unique primary-contig forward mapping to
  locate the context, then verifies it against the actual reference sequence.
  Mitochondrial GRCh37/38 coordinates share rCRS and bypass UCSC liftover; the
  actual mitochondrial reference context must still verify.
- `bcftools norm` left-aligns and verifies DNA alleles. Conflicting distinct
  allele definitions at the same normalized position are excluded.
- Alignments use a separate indel-enabled `bcftools mpileup` stream and constrained
  known-allele `bcftools call -m -C alleles`. This calls reference homozygotes as
  well as alternate/heterozygous indels; no reference genotype is inferred merely
  because an indel is absent.
- VCF input must explicitly contain the matching DNA alleles and a valid genotype.
  Absent, unsupported, conflicting or quality-rejected calls stay missing.
  The existing GQ/DP/FILTER options apply to the indel call itself. Enabled rsID
  identity rejection checks both the manifest marker and input record IDs;
  a later duplicate cannot restore a rejected call.
  Unindexed VCF/BCF input is sorted and indexed in an invocation-local
  `IndelInput_*` directory, never beside the original input.
- Verified vendor layouts are 23andMe, AncestryDNA and MyHeritage. `I` means the
  longer sequence and `D` the shorter one, regardless of which is the reference.
  Diploid homozygotes are `II`/`DD`; unphased heterozygotes are canonically `DI`.
  Ancestry writes separate allele columns; MyHeritage writes its quoted CSV.
  23andMe uses single-letter calls at haploid loci.
- Real donated exports verify these encoding tokens. Ancestry exports contain
  both `ID` and `DI`; canonical unphased `DI` is **not** a claim to reproduce its
  proprietary per-probe allele order. Without a verified exact vendor definition,
  no order or private `i…` DNA sequence is invented.

The SNP CombinedKit/calling stream remains separate. Only explicitly bound,
manifest-defined indel rows are overlaid into the selected vendor exports.
Known manifest I/D IDs with mismatched coordinates or unresolved definitions
remain missing; unrelated rows retain their existing behavior. This does not
certify or resolve every proprietary probe missing from a supplied manifest.

An invocation-local `Indels_*` directory retains normalized definitions, calling
VCF, `probe-map.tsv` (normalized alleles and original template rows),
`indel-calls.tsv`, and `summary.json` with counts and manifest SHA256 hashes.
These outputs can contain genotypes; keep them private. Source manifests are
not bundled or automatically downloaded, and their original licence/terms apply.
No new executable dependency is needed beyond the existing microarray tools.
`--call-reasons` still describes the unchanged SNP CombinedKit, not this separate
I/D table. Manufacturer bindings are run-specific CLI arguments.

## Evidence and limits

The public Illumina
[GSA v3 manifest downloads](https://support.illumina.com/array/array_kits/infinium-global-screening-array/downloads.html)
provide real sequence contexts for 10,118 I/D probes. A research audit of
GSA-24v3-0 A1 (archive SHA256
`35036a85eb79db22bec068ef69caba68f8ba9cdd1b1eef0e3f901b259e3b49af`)
found strict marker/coordinate overlaps with the existing 23andMe, Ancestry and
MyHeritage templates: 1,643 with `23andMe_V5_2025`, 3,520 with
`Ancestry_V2_2024`, and 448 with `MyHeritage_GSA_2025`, before reference-context
verification. That is evidence for a conservative subset, **not** proof
that this generic manifest describes all customized vendor probes/revisions.
In particular, its probe ordering did not reproduce every tested Ancestry
heterozygote. It is not redistributed as a certified vendor definition.

Native-tool tests exercise all three diploid genotypes from actual synthetic
aligned reads, reference/alternate length reversal, reverse-complement contexts,
manifest conflicts, quality masking and explicit unsupported VCF records.
Chromosome-only validation uses public HG002; no whole-genome validation is
necessary to enable this opt-in path. Without `--indel-manifest`, existing
consumer and AADR output behavior and paths are unchanged.
