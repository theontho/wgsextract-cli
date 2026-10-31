# CHM13 v1 coordinate asset

Generated coordinate metadata, not sample genotypes or manufacturer probe
definitions. The installed marker-only template union and pinned AADR v66.p1
panels provide 2,862,500 GRCh37/rCRS sites. Existing template/AADR provenance
applies to those inputs; upstream reference, chain and marker checksums are
recorded in `manifest.json`.

The source-allele census covers all 1,233,013 pinned AADR marker rows: none
has non-SNV source alleles. Vendor templates are marker-only, so their probe
types are unknown; no manufacturer indel manifest is bundled. The manifest
records sparse non-SNV exclusions and counts. User-provided known I/D/non-SNV
markers are separately counted and always no-called by the SNP-only T2T path.

The supported reference is the official CHM13v2.0 **original-MT, masked-Y**
analysis set (`chm13v2.0_maskedY.fa.gz`, MD5
`bd90ddb80c86af7fcfeefe7a0909b175`). Its 25 primary sequence signatures match
the public AnVIL HG00096 CRAM. Other reference variants are not interchangeable.

* `chm13-sites-*.bin.gz`: bounded delta/varint source-sorted coordinates,
  reciprocal status, orientation, REF diagnostics and native lookup permutation.
  Compressed SHA256:
  `7f6cabc29769f400cfb0879d24f828d79a1de2aed843520f776bbb6694309b35`.
* `mt-alignment-*.fa.gz`: pinned rCRS/original-CHM13 sequences and the
  origin-normalized mitochondrial alignment. Coordinates map back to the real
  native circular origin; ambiguous/N/gapped pairs are not called.
* `manifest.json` and `manifest.json.sha256`: input provenance, exact reference
  profile, PARs, codec limits and both compressed/decoded integrity records.

The independent GRCh38 reciprocal eligibility table is developer validation
output and is intentionally not packaged. Regeneration is documented in
`docs/microarray-chm13.md`; no runtime download, database or writable cache is
used. Marker-only IDs do not certify every vendor probe as an SNV.
