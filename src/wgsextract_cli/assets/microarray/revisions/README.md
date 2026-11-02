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
estimate's error, so a closely related revision could be the right one. An
identical marker list is decisive even on other coordinates (another build, a
lifted-over file), unless a second revision has that list too. A
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

## Strand quirks (strand.tsv)

Vendors declare forward-strand GRCh37 calls, but some public exports reported
certain markers on the reverse strand. `strand.tsv` lists them per revision;
`microarray --quirks vendor` writes those calls the same way for the
templates they apply to. Columns:

- `revision`, `id`, `chrom`, `position`: the catalogued revision and its
  marker, at the GRCh37 position it reports.
- `reported`, `forward`: the allele pair the exports report, and its
  complement, which GRCh37's forward strand reads.
- `fixed_in`: the first later revision of the same vendor (by export period)
  that reports the same marker at the same position on the forward strand, or
  `.`. FTDNA exports carry no dates, so their revisions cannot be ordered.
- `templates`: the `--formats` templates that write this marker reversed in
  vendor mode, or `.`. A template stands for its `exact`/`same-markers`
  revision, otherwise for every public revision it is the closest template
  to; it reproduces the quirk only when it has the marker at that position
  and each of those revisions that reported the marker on a readable strand
  reported it reversed and confirmed.
- `exports`: how many of the revision's exports called the marker.
- `confirmed_by`: the revisions that report the same site on the forward
  strand with the complementary pair.

Only evidence derived from the public exports is used, never guesses: an
entry is a marker that at least 10 of a revision's exports called with
exactly two alleles, neither of them GRCh37's base but one its complement,
and that another revision (any vendor) reports at the same site with the
complementary pair, containing the reference base, at a reference-allele
frequency within 0.25 of it. Not detectable, so not listed:

- A/T and C/G markers, which read the same on both strands (see
  `--strand-ambiguous no-call`), and markers reported with one allele, which
  could be a fixed alternate allele rather than a reversed reference.
- Reverse-strand reports no other revision confirms, and those in revisions
  with fewer than 10 calling exports or no GRCh37 exports (NCBI36 revisions;
  the FTDNA_V1_Affy template already complements calls in regions inverted
  since NCBI36).
- Swapped or otherwise wrong probe alleles: aggregate counts can't tell one
  from a multiallelic or rare-allele site, so none are listed.
- 23andMe's Yoruba-position MT markers, which no public export contains;
  `corrections.tsv` relocates them in both modes, and the loader rejects a
  strand entry for a marker it relocates, so the two cannot both apply.

The 2017-12-08 dump gives 264 entries over 16 revisions (107 sites). Vendor
mode writes 32 template markers reversed: 23andMe_V2 2, 23andMe_V3 5, Ancestry_V1 16 (10
of them, on chrX and chrY, fixed in Ancestry_V2), Ancestry_V2 8 and FTDNA_V2
1. 27 entries fall in regions hg18ToHg19 maps from NCBI36's other strand,
which may be why. Other revisions' entries are documented but not
reproduced, since no template stands for them alone.

Sources:

- The openSNP data dump of 2017-12-08 (above): allele counts over up to 100
  GRCh37 exports per revision whose sites (`sites_sha256`) match it exactly.
  Only aggregate counts are kept.
- GRCh37 as the 1000 Genomes Project's hs37d5
  (ftp.1000genomes.ebi.ac.uk/vol1/ftp/technical/reference/phase2_reference_assembly_sequence/hs37d5.fa.gz).
- The vendors' own file headers, which declare forward-strand GRCh37 calls
  (23andMe: "oriented with respect to the plus strand on the human reference
  sequence", build 37; AncestryDNA: "forward (+) strand", build 37).
- Greshake B. et al. (2014) openSNP: a crowdsourced web resource for personal
  genomics. PLoS ONE 9(3): e89204, doi:10.1371/journal.pone.0089204.

Rebuild:

```
python scripts/microarray_strand_quirks.py alleles tmp/strand --corpus tmp/opensnp
python scripts/microarray_strand_quirks.py build tmp/strand \
    --reference hs37d5.fa.gz --templates REFLIB \
    --ncbi36-chain REFLIB/hg18ToHg19.over.chain.gz
```

`alleles` streams each revision's exports from the dump; `build` compares the
counts with GRCh37 and writes `strand.tsv`, plus `candidates.tsv.gz` (every
revision's report at each candidate site) and `build-summary.json` in the
work directory.

## Rebuild the catalogue

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
