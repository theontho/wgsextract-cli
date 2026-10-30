# AADR research-panel export

```sh
wgsextract microarray --input sample.bam --ref reference \
  --formats aadr_1240k,aadr_ho,aadr_1240k+ho \
  --aadr-sample-id sample01 --aadr-sex U --region chr20 --outdir out/aadr
```

Each selected panel writes **both** PLINK `.bed/.bim/.fam` and EIGENSTRAT
`.geno/.snp/.ind` files. PLINK's SNP-major binary format is the standard compact
interchange form accepted by PLINK and ADMIXTOOLS `convertf`; unlike `.ped`, it
does not expand a single sample's million markers into a huge text row.
No PLINK/ADMIXTOOLS installation is required to write these files.

The pinned AADR v66.p1 lists are GRCh37: 1,233,013 1240K markers and 584,131 HO
markers. In this release HO is a strict subset, so `aadr_1240k+ho` (also
`aadr_1240k_ho`) has the same markers as `aadr_1240k`.
[Provenance, licence and hashes](../src/wgsextract_cli/assets/microarray/raw_file_templates/README.md#aadr-v66p1-aadr_1240k-aadr_ho-aadr_1240kho)
identify the exact public SNP-only sources.

## Coordinates and allele coding

- `.snp` preserves AADR order, chromosome codes (X=23, Y=24), genetic positions
  in Morgans, GRCh37 positions, and reference/variant alleles. `.bim` uses the
  same order with A1=AADR reference and A2=AADR variant, converting Morgans to
  PLINK's centimorgans (column 3 multiplied by 100).
- EIGENSTRAT `.geno` encodes **copies of AADR's reference allele**: 2=ref/ref,
  1=ref/variant, 0=variant/variant, 9=missing. It is not ALT dosage.
- PLINK `.bed` uses the documented SNP-major header `6c 1b 01` and the matching
  A1/A2 encoding: binary 00=ref/ref, 10=heterozygous, 11=variant/variant,
  01=missing, with unused sample bits zero.
- Calls are matched by normalized physical coordinate, not by rsID alone.
  An allele outside the AADR reference/variant pair is missing; **no strand
  guessing**, including at A/T and C/G markers, is performed.
  For VCF input, conflicting duplicate records and explicitly unsupported calls
  (including insertion genotypes) are masked directly from the per-record VCF
  calls, before any CombinedKit reference-filling assumption can affect AADR.
  This masking only removes calls; it never restores a quality-rejected call.
- GRCh38 input uses the forward GRCh37-to-GRCh38 UCSC chain to locate each AADR
  marker. Only unique primary-contig hits are used; inverted mappings explicitly
  complement the call back to the AADR strand. Output remains GRCh37. Unmapped
  and ambiguous markers are missing. Unknown builds and T2T are not supported.
- Haploid calls are represented as homozygous diploid dosage, as in the
  CombinedKit; this is **not random pseudo-haploid sampling** for ancient DNA.
- A region-limited run still writes the complete selected panel, with missing
  calls outside the region. All markers without alignment calls are missing.
  The existing VCF-input semantics apply: by default, an absent variant on a
  represented contig may be treated as homozygous reference. A variant-only VCF
  is not evidence of coverage; use an alignment for coverage-aware no-calls.
- The default sample label is `sample`, sex `U`, population `Unknown`, no parents
  or phenotype. Set a unique non-identifying `--aadr-sample-id` before merging.
  `--aadr-sex` changes metadata only, not the caller's ploidy inference.

The AADR target and calling intermediates are retained in a unique
`AADR_*` subdirectory of the output directory for each invocation, isolating
concurrent samples/builds/panels. The chain is reused read-only from the reference
library when installed, otherwise downloaded into that invocation's directory.
The intermediate CombinedKit and calling VCF also stay in that directory;
the selected PLINK/EIGENSTRAT and vendor exports are written at the output root.
Legacy cached consumer-chip targets are not
modified. Mixing AADR and vendor formats adds the AADR sites to the run's target;
`all,aadr_ho` explicitly includes the existing combined consumer target too.
An explicit `--ref-vcf-tab` contributes additional sites to an AADR run.
Without an AADR format, the existing target and output paths are unchanged.

## Compatibility validation

The authoritative allele/coding specification is
[ADMIXTOOLS convertf/README](https://github.com/DReichLab/AdmixTools/blob/master/convertf/README)
(EIGENSTRAT and PACKEDPED), and
[PLINK BED format](https://www.cog-genomics.org/plink/1.9/formats#bed).
The export preserves the **AADR reference allele**, even if another tool would
reorder alleles by frequency. Use PLINK's `--keep-allele-order` when preserving
this coding is important.

Validation uses the public GIAB HG002 GRCh38 chr20 alignment, first at
chr20:10,000,000–12,000,000, then all of chr20. It checks generated `.snp`
metadata and `.bim` allele columns against **all rows** of the pinned public
AADR `.snp` lists, and verifies that `.bed` and `.geno` encode the same calls.
This is the metadata/allele-concordance alternative to a `mergeit` genotype
merge: no AADR individuals' genotypes are needed or downloaded.
Actual downstream merging is still subject to the other dataset's build,
sample-label, SNP-selection and ploidy settings; no claim is made to reproduce
ancient-DNA pseudo-haploid processing.

The chromosome validation checked every SNP metadata row with **100% exact
ID/position/map/allele concordance** against the public sources. Nonmissing calls
also agree with the source caller VCF's AADR-reference dosage and the independently
decoded PLINK BED representation:

| Region (GRCh38) | 1240K called and verified | HO called and verified | 1240K+HO called and verified |
|---|---:|---:|---:|
| chr20:10,000,000–12,000,000 | 1,179 | 684 | 1,179 |
| chr20 | 30,371 | 16,257 | 30,371 |

One full-chr20 call has an out-of-panel allele and is correctly missing rather
than forced into AADR's biallelic coding. Both runs preserve **23/23 legacy
output files byte-for-byte** (21 vendor formats plus both CombinedKit builds)
against main `99eb6df`. The built wheel is about 50.8 MB and contains the
marker asset with the documented SHA256.

Focused tests cover reference dosage, missing/third alleles, haploid calls,
conflicting coordinates, panel selection, exact BED bytes, safe metadata,
inverted/ambiguous liftover and deterministic genotype-free asset generation.
Existing consumer formats are additionally compared byte-for-byte with main
on the same public chromosome input.

## Targets not fabricated

MTHFR Genetics, Genera and meuDNA need a verifiable real export or public
complete marker list, schema, build, strand and reuse terms. No such input was
verified in the available local inventory. GEDmatch already accepts the
combined 23andMe-style kit; no new fixed GEDmatch panel was verified.
These are not aliases to unrelated vendors and are not invented templates.
