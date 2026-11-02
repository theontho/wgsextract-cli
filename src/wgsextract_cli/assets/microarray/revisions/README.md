# Microarray chip-revision catalogue

`catalogue.json` lets `wgsextract info chip` say which vendor chip revision a
consumer raw-data file is. Vendors change a chip's marker content without
renaming the chip ("23andMe v3", "AncestryDNA V2.0"), so the header alone
cannot tell revisions apart; the marker list can.

## What an entry holds

Each revision is a marker list, described only by derived fingerprints:

- `markers`: number of marker rows.
- `ids_sha256`: SHA-256 of the sorted marker IDs, one per line. Equal for
  two files exactly when they have the same marker multiset.
- `sites_sha256`: the same over `ID<TAB>chromosome<TAB>position` (vendor
  chromosome names normalized), which separates genome builds and coordinate
  corrections of an otherwise identical list.
- `sketch`: a bottom-256 MinHash sketch (the 256 smallest BLAKE2b-64 hashes of
  the distinct IDs, base64, big-endian), to estimate how similar an unseen list
  is to this revision.
- `vendor`, `chip`, `revision`, `build`: the label. `chip` follows the vendor's
  own name; `revision` describes the list (its size). These are not
  manufacturer revision names, which vendors don't publish.
- `kind`: `export` (a list exactly as vendors export it), `reference-library`
  (a WGS Extract reference-library template) or `composite` (a union of chips,
  never a real file, so never detected).
- `declares_vendor`: whether this vendor's files always name the vendor in
  their header. A file without such a header is not matched to these revisions.
- `template`: the `--formats` template the revision is (`exact`: identical
  markers and coordinates; `same-markers`: identical markers, other
  coordinates) or, for a revision without its own template, the closest one,
  with the markers the revision adds to and removes from it.
- `source`, `exports`, `period`: where the list comes from, and how many public
  exports in the corpus have exactly this list, exported in which months.

No genotypes, names, kit or user IDs, file names or header text are stored.

## How detection scores a file

An identical marker multiset scores 1 (`exact` when coordinates also match,
otherwise `markers`). Any other file is scored by the Jaccard similarity of its
marker-ID set to each revision: exactly, against the revision's installed
template, or estimated from the sketch (about ±0.03 at 0.9, marked `~`).
Confidence is the best score, halved when the runner-up is within the
estimate's error, so a closely related revision could be the right one. A
header naming the vendor restricts candidates to that vendor.

## Sources

- The `--formats` templates: the packaged ones (see
  [raw_file_templates](../raw_file_templates/README.md)) and the WGS Extract
  reference library's.
- The openSNP data dump of 2017-12-08, CC0 1.0
  (https://archive.org/details/opensnp_data_dumps, SHA-1
  `c266c3d53049c41817eb95b608e547321bb35feb`): its 23andMe, AncestryDNA and
  FTDNA uploads. A revision is catalogued from it only when at least three
  exports share its exact marker list and it has at least 100,000 markers.

The dump has 3,393 such uploads: 3,323 were fingerprinted (147 distinct marker
lists) and 70 are not readable raw exports (corrupt archives, images,
spreadsheets, two uploads over 64 MB). FTDNA files name neither vendor nor
build, so their entries were labelled from their closest template and their
coordinates. Some FTDNA downloads concatenate the autosomal and X files, so
their column labels repeat mid-file; they are not counted as markers. A list seen only once or twice is left
out; it may be an edited file rather than a vendor revision.

What the corpus shows: same-named chips were exported with many marker lists.
23andMe v3 has 15 (960,520 to 1,001,422 markers, two of them with 960,613),
v4 has 9 (598,897 to 612,647; two with 610,545) and v1/v2 has 7 (one is the
template's); FTDNA's OmniExpress downloads have 13, most lacking 18,000-30,000
of the template's markers. AncestryDNA V1.0 and V2.0 uploads match their
templates exactly.

## Rebuild

```
python scripts/microarray_chip_catalogue.py corpus tmp/opensnp
python scripts/microarray_chip_catalogue.py survey tmp/opensnp --templates REFLIB
python scripts/microarray_chip_catalogue.py build tmp/opensnp --templates REFLIB \
    --spec scripts/microarray_chip_revisions.spec.json
```

`corpus` streams the dump with HTTP range requests (no 21 GB download) and
keeps one fingerprint per file. `survey` lists the observed marker lists with
their closest templates, to label them in the spec; `build` writes this
catalogue. `REFLIB` is a reference library with its microarray templates.
