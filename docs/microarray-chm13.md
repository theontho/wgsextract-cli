# CHM13 microarray projection

The Phase A alignment caller is SNP-only. Supplied `--indel-manifest` bindings
are exclusion-only on CHM13: known I/D/non-SNV IDs remain no-calls even when
their sequence context or coordinates cannot be verified. Valid GRCh37
positions and known non-SNV source alleles also prevent reference filling.
Vendor aliases are masked independently of the CombinedKit's first ID.
The packaged source-allele summary and per-run `asset_summary` report these
exclusions; marker-only vendor assets do not certify probe types. Alignment
indel calling is deferred to [#220](https://github.com/theontho/wgsextract-cli/issues/220).

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
SHA256 sidecar, not SQLite or a writable cache. The independent GRCh38 eligibility
table is validation-only and is not shipped. Direct GRCh37↔CHM13 mappings remain
authoritative when the GRCh38 route disagrees.

The v1 union contains 2,862,500 sites (2,392,384 legacy sites plus the pinned AADR
union). Its compact payload is 6,802,944 compressed / 16,621,310 decoded bytes.
Runtime limits are 7 MiB compressed, 17 MiB decoded and 3,000,000 rows. Changing
these measured budgets requires an explicit asset update, not a writable cache
or an automatic download.
