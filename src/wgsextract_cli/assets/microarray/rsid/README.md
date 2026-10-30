# Compact dbSNP identity history

`dbsnp157.tsv.gz` contains public rsID relationships/statuses only, with no
genotypes, sample information, annotations or citation text.

Source: [NCBI dbSNP build157, January 2025](https://ftp.ncbi.nlm.nih.gov/snp/latest_release/release_notes.txt),
[JSON archives](https://ftp.ncbi.nlm.nih.gov/snp/latest_release/JSON/).
See [NCBI data-use policies](https://www.ncbi.nlm.nih.gov/home/about/policies/).
The older organism-specific `RsMergeArch.bcp.gz` is 153,003,470 bytes and dated
February 2018; it is not the source of this current history.

| Archive | Bytes | SHA-256 |
|---|---:|---|
| `refsnp-merged.json.bz2` | 813,797,312 | `3ea8d75ecd89d87c571784a74ba09099806ec817eff9dd015a6c3874048dafee` |
| `refsnp-withdrawn.json.bz2` | 73,713,006 | `ad50ed4877bab0a39209effb75585aa51c71a189d022427ca5784706cd671679` |

Both archive MD5s were independently checked against NCBI's adjacent checksum
files. They contain 21,585,672 merged and 6,854,924 withdrawn records.

## Coverage and semantics

The 21 existing vendor layouts, including reflib v9 and packaged templates,
contain **2,354,065 distinct numeric rsIDs**. Of these, 6,012 appear in the
history archives: 5,314 merged, 694 withdrawn, two unresolved and two ambiguous.
The table additionally includes visited historical aliases, giving **6,083 rows**:
5,375 merged, 704 withdrawn, two unresolved and two ambiguous.

Columns are `RSID`, `CURRENT_RSID` (or `.`), `STATUS`. Merges are resolved
transitively to terminal identities. Multiple paths are accepted only when they
converge; divergent paths are ambiguous. Missing destinations, cycles and chains
over 64 steps are unresolved; withdrawn destinations stay withdrawn. No inference
about probe alleles or orientation is made.

This is **not the full dbSNP catalogue**. Unlisted IDs are unchanged, not certified
active; unrelated external aliases are outside its scope. Additions to vendor
templates require refreshing the subset. AADR uses coordinate matching, not this
vendor-rsID fallback.

The deterministic gzip is **51,307 bytes**, SHA-256
`86e5c13de494e1ddf67edc8e905d9c38919502322581e8131d923d0ec12caedc`.
The feature loads this wheel asset offline, with no added dependencies/downloads.

## Developer refresh

Download and verify the two pinned archives into a local `out/` directory.
The developer-only stdlib builder validates their SHA-256s before processing:

```bash
python scripts/build_microarray_rsid_map.py \
  --archives out/dbsnp157/inputs --work-dir out/dbsnp157/build \
  --templates /path/to/reflib/microarray/raw_file_templates \
  --output out/dbsnp157/dbsnp157.tsv.gz
```

Packaged template bodies are included automatically; `--templates` is repeatable.
Building the full temporary identity graph uses approximately 514 MB of disk in
the chosen work directory; the compressed source archives total 887.5 MB.
`--reuse-graph` accepts a previously verified local graph and checks its record
counts. Never supply an untrusted/stale graph. No graph or source archive ships
with the CLI. Review the summary and update this provenance before copying a
refreshed table into the package.
