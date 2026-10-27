# Microarray templates shipped with the CLI

Most templates come with the reference library (`ref bootstrap`). These are the
ones it lacks; `microarray` searches here last. A body file holds one marker per
line (rsID, chromosome, position, tab-separated) and is gzipped; the head file
is written as-is at the top of the output.

## 23andMe_V2 (`--formats 23andme_v2`, also `23andme_v1`)

- Source: openSNP data dump 2017-12-08, CC0 1.0
  (https://archive.org/details/opensnp_data_dumps), member
  `user6045_file4542_yearofbirth_unknown_sex_unknown.23andme.txt`, a 23andMe
  export of 2017-05-26 on build 37 (574,518 markers). Only marker IDs and
  positions are kept, no genotypes.
- Every build-37 v1/v2-era export in the dump with this marker count has the
  identical list, and the files don't name their chip; within an export period
  v1 and v2 files share one list, so `23andme_v1` is an alias.
- Rebuild: `python scripts/microarray_template_from_raw.py <raw file> 23andMe_V2`.

## 23andMe_V5_2025, Ancestry_V2_2024, Nucleus_V1

`--formats 23andme_v5_2025`, `ancestry_v2_2024`, `nucleus`: the chips as their
vendors export them today, for services that check a file against current
exports.

- Source: raw data exports contributed by the project's maintainer: a 23andMe
  v5 export of January 2025 (643,161 markers), an AncestryDNA V2.0 export of
  November 2024 (677,436 markers), and a Nucleus array export of 2025 (665,107
  markers). Only marker IDs and positions are kept, no genotypes; each
  vendor's exports for different people have identical marker lists.
- 23andMe's head has the export's CRLF line endings, and its link to the
  customer's account is replaced by the generic download page. Nucleus exports
  have no header, so there is no head file.
- Rebuild: `python scripts/microarray_template_from_raw.py <raw file> <NAME>`.

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
