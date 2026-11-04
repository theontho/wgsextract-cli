# Variant table: query your variants with SQL

`wgsextract vcf table` turns an annotated VCF into a flat table with one row per
variant allele and fixed, typed columns. Spreadsheets, `sqlite3`, DuckDB,
pandas or an AI agent can read it without knowing the VCF format.

```bash
wgsextract vep --input sample.vcf.gz                       # gene, consequence, HGVS
wgsextract vcf clinvar --vcf-input sample_vep.vcf.gz       # ClinVar significance
wgsextract vcf gnomad --vcf-input clinvar_annotated.vcf.gz # gnomAD frequency
wgsextract vcf table --vcf-input gnomad_annotated.vcf.gz
```

This writes `<input stem>_variants.tsv` and `<input stem>_variants.sqlite`
(`--no-sqlite` skips the database) and a result manifest naming both. The
table uses whatever annotations the VCF has. Columns whose annotation is
missing stay empty, so annotate first; any subset and order of the commands
above works.

## Rows

- **One row per ALT allele.** A multi-allelic site gives a row per ALT, each with
  its own allele frequency, consequence and zygosity. Rows keep the VCF's
  order, which is sorted by position.
- **Only alleles the sample carries.** Calls of `0/0`, no-calls and the other ALT
  of a `0/1` multi-allelic site are left out, so a gVCF or a DeepVariant VCF
  with `RefCall` records yields only real variants. `--all-alleles` keeps every
  ALT, with `zygosity` saying which the sample carries. A sites-only VCF (no
  samples) keeps every ALT.
- gVCF reference blocks (`<NON_REF>`, `<*>`) and the spanning-deletion
  placeholder `*` never get a row.
- A multi-sample VCF uses its first sample, or the one named with `--sample`.
- `--region chr22` or `--region chr1:1000000-2000000` exports one region.

## Columns

| Column | Type | Meaning |
| --- | --- | --- |
| `chrom`, `pos`, `ref`, `alt` | text, integer, text, text | The allele, as in the VCF (1-based position). |
| `rsid` | text | dbSNP rsIDs from the VCF's ID column, else VEP's `Existing_variation`, else PharmGKB's; `;`-separated. Callers such as DRAGEN and DeepVariant leave the ID column empty, so annotate with `vep` (the default `everything` preset) to fill it. |
| `gene` | text | VEP `SYMBOL`, else SnpEff `Gene_Name`, else the PharmGKB gene. |
| `consequence`, `impact` | text | Sequence Ontology terms (`&`-separated) and HIGH/MODERATE/LOW/MODIFIER, from the chosen transcript. |
| `hgvs_c`, `hgvs_p` | text | HGVS coding and protein change on the chosen transcript. |
| `clinvar_significance`, `clinvar_condition` | text | ClinVar `CLNSIG` and `CLNDN` (from `vcf clinvar`; else VEP's `CLIN_SIG`). |
| `gnomad_af` | real | gnomAD allele frequency of this ALT (from `vcf gnomad`; else VEP's `gnomADg_AF`, then `gnomADe_AF`, then `gnomAD_AF`). |
| `genotype`, `zygosity` | text | The sample's GT (`0/1`, `1\|1`, ...), and its copies of this ALT: `het`, `hom_alt`, `hemizygous`, `not_carried` or `no_call`. |
| `depth`, `gq`, `qual`, `filter` | integer, integer, real, text | Read depth (FORMAT/DP, else the sum of AD, else INFO/DP), genotype quality, QUAL and FILTER. |
| `confidence` | text | The depth-aware `high`/`moderate`/`low` label in `INFO/WGSE_CONF`, when the VCF has one. |
| `pharmgkb_level`, `pharmgkb_drugs` | text | Best PharmGKB level of evidence (1A, 1B, 2A, 2B, 3, 4) and the drugs, from `vcf pharmgkb`. |
| `pharmcat_diplotype`, `pharmcat_phenotype` | text | PharmCAT's call for the row's gene, with `--pharmcat-json <stem>.phenotype.json` from `vcf pharmcat`. |

The TSV has a header row and leaves missing values empty. The SQLite database
has the table `variants` with these columns, `columns` (each column's type and
meaning, for an agent to read first) and `metadata` (wgsextract version,
source VCF and its SHA-256, sample, reference build, region, annotations found,
row count and command line). It is indexed on `gene`, `rsid` and
`(chrom, pos)`.

**Which transcript.** VEP annotates every transcript a variant touches. The
table keeps one per allele, as VEP's `--pick` would: the one VEP flagged
`PICK`, else the MANE Select transcript, then the canonical one, then a
protein-coding one, then the most severe consequence. SnpEff lists the most
severe effect first, and the table takes that one.

## Example queries

With `sqlite3 sample_variants.sqlite` (or any SQLite client):

```sql
-- What the database holds, and where it came from
SELECT name, type, description FROM columns;
SELECT key, value FROM metadata;

-- Variants in a pharmacogene
SELECT chrom, pos, ref, alt, rsid, consequence, hgvs_p, genotype
FROM variants WHERE gene = 'CYP2C19';

-- One rsID
SELECT * FROM variants WHERE rsid = 'rs4244285';

-- A region (uses the (chrom, pos) index)
SELECT * FROM variants
WHERE chrom = 'chr17' AND pos BETWEEN 43044295 AND 43125483;

-- ClinVar pathogenic or likely pathogenic, well covered
SELECT chrom, pos, ref, alt, gene, clinvar_significance, clinvar_condition,
       zygosity, depth
FROM variants
WHERE (clinvar_significance LIKE 'Pathogenic%'
       OR clinvar_significance LIKE 'Likely_pathogenic%')
  AND depth >= 20
ORDER BY gene;

-- Rare protein-changing variants (absent from gnomAD or below 0.1%)
SELECT gene, hgvs_p, consequence, gnomad_af, zygosity
FROM variants
WHERE impact IN ('HIGH', 'MODERATE')
  AND (gnomad_af IS NULL OR gnomad_af < 0.001)
  AND filter = 'PASS';

-- Homozygous loss-of-function candidates
SELECT gene, hgvs_c, hgvs_p, consequence
FROM variants WHERE impact = 'HIGH' AND zygosity = 'hom_alt';

-- Variants per gene, most first
SELECT gene, count(*) AS n FROM variants
WHERE gene IS NOT NULL GROUP BY gene ORDER BY n DESC LIMIT 20;

-- Drug-response variants with strong PharmGKB evidence
SELECT gene, rsid, genotype, pharmgkb_level, pharmgkb_drugs
FROM variants WHERE pharmgkb_level IN ('1A', '1B');
```

DuckDB reads the TSV without loading it into a database first:

```bash
duckdb -c "SELECT gene, count(*) FROM read_csv('sample_variants.tsv', delim='\t')
           WHERE impact = 'HIGH' GROUP BY gene ORDER BY 2 DESC"
```

and pandas: `pd.read_csv("sample_variants.tsv", sep="\t")`, or
`pd.read_sql("SELECT * FROM variants WHERE gene = 'BRCA2'", sqlite3.connect(...))`.

## Asking an agent

Point an AI agent at the SQLite file and ask in plain language. Tell it to read
the `columns` and `metadata` tables first. Questions it can answer with the
queries above:

- "Do I carry any ClinVar pathogenic or likely pathogenic variants? Which genes,
  and is the read depth good enough to trust them?"
- "Which variants do I have in CYP2C19, CYP2C9 and VKORC1, and what do they mean
  for clopidogrel and warfarin?"
- "List my rare (gnomAD below 0.1%) protein-changing variants, grouped by gene."
- "Am I homozygous for any loss-of-function variant?"
- "What is my genotype at rs429358 and rs7412 (APOE)?"
- "How many variants of each consequence type do I have?"

An agent's reading of a variant is not a diagnosis. ClinVar classifications
change, a low-depth or low-confidence call can be wrong, and the table holds
only what was called and annotated. Confirm anything that matters with an
accredited laboratory and a genetic counsellor.

## Size and speed

A 30x whole-genome DRAGEN VCF of 4.98 million records (5.07 million ALT
alleles) annotated with `vcf clinvar` took 2 minutes 14 seconds on one CPU
core with a peak of 260 MB of memory. Records are streamed, so memory does not
grow with the VCF. It wrote a 306 MB TSV and a 518 MB SQLite database, in
which an indexed lookup by gene, rsID or region takes about a millisecond and a
query that scans the whole table one to three seconds.
