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
