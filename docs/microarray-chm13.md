# CHM13 microarray projection

The default alignment caller remains SNP-only. Supplied `--indel-manifest`
bindings enable a separate, conservative alignment-based I/D caller for the
bound vendor's exact GRCh37 template rows. Native VCF input remains
exclusion-only. Known I/D/non-SNV IDs remain no-calls when
their sequence context or coordinates cannot be verified. Valid GRCh37
positions and known non-SNV source alleles also prevent reference filling.
Vendor aliases are masked independently of the CombinedKit's first ID.
The packaged source-allele summary and per-run `asset_summary` report these
exclusions; marker-only vendor assets do not certify probe types.

The CHM13 path is separate from the existing GRCh37/38 implementation. It calls
native CHM13 positions and projects nucleotide genotypes to the existing
GRCh37/rCRS vendor templates and AADR panels. It does not infer a probe's strand
or convert a nucleotide call into a vendor insertion/deletion code.

## Reference requirements

Only the CHM13v2.0 analysis set with **original chrM and masked chrY PARs** is
supported. The unmasked-Y and masked-Y/rCRS analysis sets are different reference
variants and are rejected. The packaged manifest records all 25 primary sequence
lengths and MD5s, the reference/index hashes, and the published PAR intervals.

The FASTA must already be indexed. Equivalent FASTA encodings are checked through
their actual indexed sequences, not a filename or a stale sequence dictionary.
BAM/CRAM input must carry matching primary `@SQ` names, lengths and `M5`
signatures; missing signatures cannot establish which reference variant was used.
VCF input is normalized to the reference's chromosome names, checked against any
declared contig signatures, and every observed REF is checked against the actual
FASTA before projection.

The complete installed marker-template union and the installed
`hg18ToHg19.over.chain.gz` are required. No tools, templates or chains are
installed by this path. Custom `--ref-vcf-tab` and `--ploidy-file` overrides are
not supported for CHM13.

## Calls and diagnostics

Existing `--region`, `--parallel`, quality and marker-identity options apply.
Region coordinates are **native CHM13 coordinates**. Male PAR/non-PAR ploidy uses
the CHM13 intervals, not GRCh38 defaults; chrM is haploid.

* Inverted mappings complement a genotype exactly once.
* Assembly REF differences are reported, not used to rewrite a genotype.
* Nonreciprocal, ambiguous and colliding mappings are no-calls.
* Unsupported indel records, overlapping deletions, conflicting duplicates and
  rejected quality/identity calls remain no-calls; later records or AADR export
  cannot restore them.
* Marker-only vendor IDs do not certify every probe as a SNP. Public dbSNP-only
  probe/type enrichment is a separate follow-up, not a claim of this path.
  The alignment route reuses the existing SNP-only caller (`mpileup -I`,
  `call -V indels`); it cannot discover every indel allele. Observed indel
  masking applies to supplied VCF records, not an indel-probe certification.
* Variant-only VCF reference filling still honors `--assume-hom-ref`; depth/GQ
  gates do not manufacture evidence at absent sites.

Each invocation retains its native VCF, target and canonical CombinedKit in a
private `CHM13_*` output subdirectory. `<sample>_CHM13_report.json` records reason
counts, reference diagnostics and the CombinedKit path. `--call-reasons` also
writes per-site reasons beside the CombinedKit. Vendor and AADR files retain
their normal names.

## Opt-in alignment I/D probes

Use the existing `--indel-manifest FORMAT=PATH` option with an applicable
manufacturer manifest, as described in [microarray-indels.md](microarray-indels.md).
No manifest or executable is installed automatically. Binding a generic array
manifest does not certify every customized vendor probe or revision.

Only exact marker-ID **and** GRCh37-coordinate matches in the bound template
are considered. NCBI36 templates remain exclusion-only. A reciprocal,
unambiguous catalogue mapping locates a search window; the full manifest DNA
sequence on **both** sides must match exactly one native reference haplotype,
in either orientation. No sequence, probe type or I/D code is inferred from an
rsID. `bcftools norm` verifies and left-aligns the alleles. Distinct overlapping
normalized definitions are quarantined together, even across bound formats.

A separate indel-enabled, known-allele `mpileup`/`call` stream uses the
validated CHM13 ploidy and the same sex determination as SNP calling. A native
record and matching DNA alleles are mandatory, including for a reference
homozygote: an absent variant never becomes a reference call. Depth must be
at least 4 and genotype quality at least 20; existing user thresholds may
tighten these floors. For forced reference records without GQ, the complete
biallelic PL vector supplies the difference between the called and next-best
genotype likelihoods. Missing/incomplete likelihoods cannot supply quality.
Existing FILTER and rsID-identity exclusions apply and cannot be restored.

The genotype additionally requires at least **two distinct read fragments per
called allele**, spanning every repeat-equivalent placement of the normalized
event and **15 exact reference bases beyond each end of that span**. The native
reference determines the rightmost equivalent placement, including motif
rotations for tandem-repeat insertions and deletions. Both outer anchors must
occur once in the fetched local reference window and complete alternate
haplotype. An unresolved shift of 4,096 bases, non-unique anchors, missing bases
or ambiguous reference sequence stays a no-call. Reads ending inside a repeat
provide neither REF nor ALT support, even when their alignment is gapless.
Mapping and every supporting base quality must be at
least 20. Secondary, supplementary, duplicate and QC-failed reads are excluded;
overlapping mates are counted once. Partial reads and flanking mismatches
provide no support. A conflicting fragment, other fully-flanked haplotype,
or a deletion/skip overlapping the event and removing a flank quarantines
the probe. Homozygous calls with opposing fully-flanked evidence are no-calls.
Extreme depth (1,000 fragment names per probe) is conservatively quarantined.
These deliberately strict rules can reduce call rate, especially in repeats,
near other variants or at low coverage.

Ploidy must be consistent across the full flanked interval. Female chrY,
masked male chrY PARs, intervals crossing PAR boundaries or the mitochondrial
origin, and missing reference flanks are no-calls. Male non-PAR chrX/chrY and
chrM are haploid; male chrX PARs and female chrX are diploid. As in existing
SNP calling, unclassified sex defaults to female ploidy.

Only verified vendor I/D encodings are overlaid, retaining each original
template ID, coordinate and row order. SNP CombinedKit and AADR projections
stay separate and keep known indel exclusions. Without manifest bindings,
all existing CHM13 and GRCh37/38 calls and default exports are unchanged.
`CHM13_*/indels/` retains normalized definitions, native calling VCF,
`probe-calls.tsv` (including fragment counts) and a hashed-manifest
`summary.json`; the top-level CHM13 report links the summary. These
per-sample diagnostics contain genotypes and must remain private.

## Pinned mitochondrial bridge

Original CHM13 chrM has a different circular origin from rCRS. The bridge first
normalizes that origin with the unique first-40-base rCRS anchor, then computes a
global unit-cost alignment. Only reciprocal base pairs shared by **all optimal
alignments of the origin-normalized sequences** are used. Coordinates are then
converted back to the actual native CHM13 sequence. `N` costs one substitution
and never provides a called mapping. The manifest pins the rotation, scoring,
sequence checksums and alignment artifact.

The legacy GRCh38 mitochondrial target/export behavior is deliberately unchanged.

## Regeneration

`scripts/build_microarray_chm13_map.py --help` documents the developer generator.
It requires the complete marker-only templates, direct and reciprocal chains,
the independent GRCh38 cross-check chains, indexed source/native references with
sequence dictionaries, and the published CHM13 PAR BED.

The runtime asset is bounded delta-encoded arrays plus a JSON manifest and its
SHA256 sidecar. The independent GRCh38 eligibility
table is validation-only and is not shipped. Direct GRCh37↔CHM13 mappings remain
authoritative when the GRCh38 route disagrees.

The v1 union contains 2,862,500 sites (2,392,384 legacy sites plus the pinned AADR
union). Its compact payload is 6,802,944 compressed / 16,621,310 decoded bytes.
Runtime limits are 7 MiB compressed, 17 MiB decoded and 3,000,000 rows. Changing
these measured budgets requires an explicit asset update, not a writable cache
or an automatic download.

### Derived caches

Runs reuse decoded site-map columns, the validated marker-ID union, and indexed
native targets under `microarray/chm13/` in the configured reference library.
The packaged assets are never modified: their SHA256 checks still run before
using cached columns. Derived files have checksums and are published under
builder locks; missing, truncated, or corrupt files are rebuilt.

Keys include the catalogue manifest/reference profile, builder version, and
template/chain/AADR file paths, sizes, and modification times. Target keys also
include chromosome aliases and the selected native regions. Changing these
inputs invalidates the corresponding cache. These files are disposable; delete
`microarray/chm13/` to reclaim them or force a rebuild. Without a configured
reference library, setup remains uncached.
