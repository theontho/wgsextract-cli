# GRCh37/38 coordinate catalogs

The legacy microarray paths use compact, pinned coordinate catalogs for the
NCBI36 → GRCh37, GRCh37 → GRCh38 and GRCh38 → GRCh37 transformations. These
are **coordinate caches**, not new marker definitions or calling algorithms.
The existing consumer templates remain authoritative for marker IDs, aliases,
duplicates, row order and formatting. The packaged AADR marker list remains
authoritative for its panels, alleles and order. The CHM13 site map is separate
and unchanged.

Each catalog contains the complete, unfiltered marker union for its direction,
including unmapped queries and every placement in the original chain order,
with strand and chain score. Source and destination coordinates are delta
encoded in compressed, byte-lane-shuffled little-endian array columns. There are no duplicate
ordered vendor projections, genotypes or SQLite databases.

## Compatibility rules

* Existing consumer target and export paths retain their first-hit policies.
  AADR retains its stricter unique, primary-placement policy. Catalog lookup
  does not repair ambiguous placements or discard collision partners when a
  sample, contig filter or quality threshold removes other calls.
* GRCh38 target construction retains the historical rCRS mitochondrial
  identity mapping. **GRCh38 vendor export still uses the reverse UCSC
  hg38-to-hg19 mitochondrial chain**, including its Yoruba coordinate shifts
  and gaps. These intentionally different policies are not interchanged.
* FTDNA's NCBI36 output retains its sample-dependent rsID lookup when no chain
  is supplied. The catalog does not freeze this fallback into a coordinate
  mapping.
* Quality no-calls, identity rejection, indel overlays, absent-site assumptions,
  genotype complementation and haploid formatting are unchanged.
* Catalog selection requires the exact installed chain's SHA-256, not its
  filename. Custom chains use the original chain implementation. A custom
  template or target can query positions outside the packaged union; those
  queries lazily load the original chain. No network annotation or new
  configuration is involved.
* A selected catalog with a bad checksum, invalid dimensions, malformed
  coordinates or excessive decoded size fails with an explicit error rather
  than returning a potentially incorrect mapping.

## Reproduction and verification

With the project's development environment and an existing reference library
containing all consumer templates and the three UCSC chains:

```bash
pixi run -e dev python scripts/build_microarray_catalog.py \
  --reference-library /path/to/reference \
  --output out/microarray-catalog
```

The builder requires all 21 layouts, includes every current AADR marker, and
records input template, correction-table, AADR and chain checksums in its JSON
manifest. For every catalogued query it compares decoded results with the
original chain in both source-strand directions, including all placements,
scores, inversions and explicit unmapped queries. The builder does not download
or install reference data.

Before a catalog revision is released, compare complete output-file SHA-256s
against the pre-migration implementation for **all 21 consumer layouts on both
builds**, plus all three AADR panels. Run small public inputs first and then
complete-chromosome inputs. Preserve a separate GRCh38 mitochondrial oracle and
regressions for ambiguity, collision partners, custom coordinates/chains and
NCBI36's no-chain fallback. A higher call rate is not a substitute for byte
equality.
