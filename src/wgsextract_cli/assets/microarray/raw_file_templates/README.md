# Microarray templates shipped with the CLI

Most templates come with the reference library (`ref bootstrap`). These are the
ones it lacks; `microarray` searches here last. A body file holds one marker per
line (rsID, chromosome, position, tab-separated) and is gzipped; the head file
is written as-is at the top of the output.

## 23andMe_V2 (`--formats 23andme_v2`, also `23andme_v1`)

- Source: openSNP data dump 2017-12-08, CC0 1.0
  (https://archive.org/details/opensnp_data_dumps), a 23andMe
  export of 2017-05 on build 37 (574,518 markers). Only marker IDs and
  positions are kept, no genotypes.
- Every build-37 v1/v2-era export in the dump with this marker count has the
  identical list, and the files don't name their chip; within an export period
  v1 and v2 files share one list, so `23andme_v1` is an alias.
- Rebuild: `python scripts/microarray_template_from_raw.py <raw file> 23andMe_V2 --overwrite`.

## 23andMe_V5_2025, Ancestry_V2_2024, Nucleus_V1

`--formats 23andme_v5_2025`, `ancestry_v2_2024`, `nucleus`: the chips as their
vendors export them today, for services that check a file against current
exports.

- Source: private donated exports: a 23andMe
  v5 export of January 2025 (643,161 markers), an AncestryDNA V2.0 export of
  November 2024 (677,436 markers), and a Nucleus array export of 2025 (665,107
  markers). Only marker IDs and positions are kept, no genotypes; each
  vendor's exports for different people have identical marker lists.
- 23andMe's head has the export's CRLF line endings, and its link to the
  customer's account is replaced by the generic download page. Nucleus exports
  have no header, so there is no head file.
- Rebuild: `python scripts/microarray_template_from_raw.py <raw file> <NAME> --overwrite`.

## Additional MyHeritage GSA and Ancestry layouts

These are GRCh37 marker lists, not inferred manufacturer chip revisions.
MyHeritage's headers identify GSA but do not declare a GSA sub-revision;
the count-qualified ID distinguishes two genuine export layouts. Both layouts
occur in 2025, so the year is not a guarantee of which layout a kit uses.

| Format ID (`--formats`) | Vendor | Chip revision in source | Export year-month | Markers | Provenance |
|---|---|---|---|---:|---|
| `myheritage_gsa_2025` | MyHeritage | GSA; sub-revision not declared | 2025-04 | 609,346 | private donated export |
| `myheritage_gsa_2025_609424` | MyHeritage | GSA; sub-revision not declared | 2025-03 | 609,424 | private donated export |
| `ancestry_v2_2021` | AncestryDNA | V2.0 | 2021-04 | 677,864 | private donated export |

The 2021 Ancestry layout is an additional historical option, not a replacement
for `ancestry_v2_2024`. Available 2024–2025 Ancestry exports have the same
677,436-marker list as that already-shipped template. No verified original
FTDNA GSA export was available: the FTDNA files found in the inventory were
reference-template bodies, not donated vendor exports. No new FTDNA revision
is invented. Existing format IDs and marker lists remain selectable and unchanged.

Marker drift against the reference-library v9 templates is reported as Jaccard
distance: `100 × |new markers △ baseline markers| / |new markers ∪ baseline markers|`.
Chromosome aliases are normalized for coordinate comparisons; all rows below
use GRCh37. "Added" and "removed" count marker IDs relative to the baseline.

| Export layout | Baseline | Added | Removed | ID drift | ID/chromosome/position drift |
|---|---|---:|---:|---:|---:|
| MyHeritage 609,346 | `MyHeritage_V2` | 1,247 | 2,029 | 0.5358% | 0.5365% |
| MyHeritage 609,424 | `MyHeritage_V2` | 1,301 | 2,005 | 0.5407% | 0.5417% |
| Ancestry 677,864 (2021) | `Ancestry_V2` | 148,905 | 139,983 | 35.3230% | 35.6465% |
| Ancestry 677,864 (2021) | bundled `Ancestry_V2_2024` | 428 | 0 | 0.0631% | 0.0631% |
| Ancestry 677,436 (2024–2025) | bundled `Ancestry_V2_2024` | 0 | 0 | 0% | 0% |

New headers use generic vendor boilerplate and the vendor's column-label format;
no source comments, names, account links, kit/sample IDs or individual timestamps
are copied. Bodies contain exactly three columns: marker ID, chromosome and
position. No genotype or call-status filtering is used, including for no-call rows.
Unplaced markers (position zero) stay in the vendor layout and produce no-calls;
they are excluded only from the one-based genotyping target.
Gzip bodies have neither a source filename nor a timestamp in their metadata.

Rebuild with `python scripts/microarray_template_from_raw.py <private raw export>
<template name> --overwrite`; omit `--overwrite` when adding a new format.
The extractor validates the source schema and declared reference build, rejects
existing templates by default, and writes only generic headers and marker lists.
Raw exports and identifying provenance must stay outside version control.

## AADR v66.p1 (`aadr_1240k`, `aadr_ho`, `aadr_1240k+ho`)

These are research panels, not consumer-vendor templates. Their shared,
marker-only table is `../aadr/v66.p1.tsv.gz`. It retains AADR's SNP ID
(including non-rs IDs), chromosome, genetic position in Morgans, one-based
GRCh37 physical position, reference allele and variant allele, plus an HO
membership flag. The last two allele columns are **AADR's** reference/variant
coding, not an inferred ancestral allele or a call from an individual.

- Source: Mallick et al., *The Allen Ancient DNA Resource (AADR): A curated
  compendium of ancient human genomes*, DOI
  [10.7910/DVN/FFIDCW](https://doi.org/10.7910/DVN/FFIDCW),
  Harvard Dataverse dataset version **14.0**, released **2026-06-08**,
  panel release **v66.p1**; retrieved **2026-10-30**.
- Terms: the pinned Dataverse version declares **CC0 1.0**
  (<https://creativecommons.org/publicdomain/zero/1.0/>). See also the release
  README, <https://dataverse.harvard.edu/api/access/datafile/13994530>.
  Cite the AADR release and Mallick et al. (2024), *Scientific Data* 11, 182,
  DOI [10.1038/s41597-024-03031-7](https://doi.org/10.1038/s41597-024-03031-7),
  when using the resource. No `.geno`, `.ind` or individual annotations were
  downloaded or packaged.
- 1240K: **1,233,013** markers, including X and Y;
  <https://dataverse.harvard.edu/api/access/datafile/13994514>
  (`v66.p1_1240K.aadr.patch.PUB.snp`), SHA256
  `1189e253b82058822fddabc4966c1448dea70373a96c40106e720bedd93b55bf`.
- Human Origins (HO): **584,131** markers, including X and Y;
  <https://dataverse.harvard.edu/api/access/datafile/13994527>
  (`v66.p1_HO.aadr.patch.PUB.snp`), SHA256
  `c0d565ee9e6a6edf30c2063430ee4f828e5db45f26d4747fd76f19eee650a5de`.
- Every HO marker is in this release's 1240K panel, with identical ID,
  coordinates, genetic position and alleles. Consequently **1240K+HO has
  1,233,013 markers**, not the sum of the panel sizes. It is an explicit
  union target, not a third invented panel.
- The deterministic gzip asset is **14,149,959 bytes**, SHA256
  `7ecbbc06a876c57236c36b4779f881648dbe7754851c42fd12d625ada8269b05`.
  Sharing one table avoids shipping HO twice. The incremental compressed size
  is about 13.5 MiB, comparable to the existing packaged chip lists; the wheel
  remains below PyPI's standard 100 MB limit. No runtime panel download is
  needed. The UCSC GRCh37-to-GRCh38 chain, when needed, is reused read-only from
  the reference library or obtained through the existing reference-download
  helper into the run's unique output subdirectory.

Rebuild from the **two SNP metadata files only**:

```sh
python scripts/microarray_aadr_markers.py 1240K.snp HO.snp \
  src/wgsextract_cli/assets/microarray/aadr/v66.p1.tsv.gz
```

The builder rejects duplicate/out-of-order positions, invalid alleles and panel
disagreements, and validates HO subset membership before writing anything.
It preserves SNP IDs and allele order and adds no source filenames or timestamps
to gzip metadata. See [AADR export and validation](../../../../../docs/aadr.md)
for file conventions, allele checks, and compatibility limitations.

## corrections.tsv

Position fixes applied when reading the reference library's templates. 102 MT
markers in 23andMe_V4, V5 and V35 are at Yoruba (UCSC hg19 chrM) positions in
an otherwise rCRS list, so they missed the kit's calls. Each is corrected only
when a 2025 23andMe export gives the marker another position and the
template's position, lifted with hg19ToHg38's chrM chain, is exactly that.

- Rebuild: `python scripts/microarray_template_corrections.py <reflib> <23andMe raw file>`.

## FTDNA_V1_Affy (reference library)

This template's positions are NCBI36 (hg18). Its markers are lifted to GRCh37
with UCSC's `hg18ToHg19.over.chain.gz`, downloaded once into the reference
library, both for the genome-wide target and to find each marker's call.
Without the chain, calls are matched by rsID.
