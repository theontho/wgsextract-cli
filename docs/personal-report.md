# Personal report (`wgsextract report`)

`wgsextract report` summarises what earlier runs found for one sample in a
single local report. It has three parts:

- an HTML page, `<sample>_personal_report.html`;
- the same content as Markdown, `<sample>_personal_report.md`;
- the JSON document an agent or script can read, `<sample>_personal_report.json`,
  plus a findings table, `<sample>_personal_report_findings.tsv` (or `.parquet` with
  `--format parquet`).

```sh
wgsextract report --genome HG002            # a genome-library folder
wgsextract report --input HG002/HG002.cram  # the input's folder
wgsextract report --workspace HG002 --results ~/wgs-out/HG002
```

The report is built from the result manifests (`*.manifest.json`, see
[result-manifest.md](result-manifest.md)) that commands left in the sample
folder, searched recursively, and from the sample workspace
([sample-workspace.md](sample-workspace.md)). It runs no analysis. Run the
analyses first; the report shows each section that has no run as **not run**,
with the command that produces it.

When results were written elsewhere (`--outdir` or the `output_directory`
setting), name that folder with `--results DIR`. The report's own output
folder is always read. Only runs whose inputs are files in the sample folder,
or outputs of runs already used, are taken from those folders, so a shared
output folder doesn't mix samples. When a command ran more than once, the
newest successful run is used. A section whose only runs failed is shown as
**failed**, with the error.

> **Technical result, not diagnosis.** The report describes what software
> found in this sequencing run. It is not a diagnosis or medical advice.
> Confirm any finding with an accredited clinical laboratory before acting on
> it.

## Sections

| Section | Source runs | Findings |
| --- | --- | --- |
| Sample overview | workspace facts (sex, platform, build, coverage, depth by target, read length, each with its method and confidence); `info --detailed` (QC metrics, plus sex, coverage and read length with confidence "not recorded" when the folder has no workspace) | none: facts and QC tables |
| Lineage | `lineage y-haplogroup`, `lineage mt-haplogroup` | the Y and mtDNA haplogroups, with tree links (YFull, FTDNA). Y-DNA is not asked for when the recorded sex is Female. |
| Pharmacogenomics | `vcf pharmcat`, `typing cyp2d6` (Cyrius) | one per PharmCAT gene (diplotype and phenotype); the CYP2D6 diplotype |
| ClinVar | `vcf clinvar` | each carried ClinVar pathogenic or likely-pathogenic call in an ACMG SF v3.2 gene |
| Microarray export | `microarray` | none: the files produced, with sites, called sites and no-call rate |

Warnings the source runs recorded (low coverage, high no-call rates, ...) are
shown with their section.

### ClinVar gene list

ClinVar hits are listed only for the 81 genes of the ACMG secondary-findings
list, version 3.2 (Miller DT et al., *Genet Med* 2023;25(8):100866, PMID
37347242). The genes, categories and notes were checked against the paper's
[Table 1](https://pmc.ncbi.nlm.nih.gov/articles/PMC10524344/). The genes and
their hg19/hg38 spans are in
`src/wgsextract_cli/assets/report/acmg_sf_v3.2.tsv`. A call is placed in a
gene by position, in the build of the VCF `vcf clinvar` read. The section
also counts the P/LP calls outside the list and links the full table of every
carried call (`clinvar_pathogenic.confidence.tsv`).

ACMG reports some genes only in particular cases. For MUTYH, CASQ2, TRDN, BTD,
GAA, ATP7B and RPE65 only two P/LP variants (biallelic) count, and for HFE only
p.C282Y homozygotes. TTN counts only truncating and high-PSI splicing variants,
RYR1 only malignant-hyperthermia variants, and LMNA not missense variants never
seen with cardiac disease. Those findings carry this as a note. The report does
not work out phase, variant consequence or which HFE variant a call is.

## Confidence labels

Every finding has a label, `high`, `moderate`, `low` or `not_assessed`, with
reason codes. The JSON document explains each code in `confidence_reasons`.

- **ClinVar**: the call's own label from `vcf clinvar`
  ([variant-confidence.md](variant-confidence.md)): depth, genotype quality,
  allele balance, mapping quality and strand bias.
- **PharmCAT**: the weakest label among the gene's labelled calls in the
  run's confidence table. A gene with no labelled calls (a reference `*1/*1`
  rests on coverage alone) is `not_assessed`.
- **CYP2D6 (Cyrius)**: `low` when Cyrius made no call or its filter did not
  pass; otherwise the median depth against the `confidence_min_depth` and
  `confidence_high_depth` settings (`low` below the minimum, `moderate`
  below the high threshold, `high` above it).
- **Haplogroups**: the caller's quality score (Yleaf QC-score, Haplogrep
  quality): `high` from 0.9, `moderate` from 0.7, `low` below 0.7. A run that
  warned of low coverage is `low`.

## Evidence and privacy

Each finding names its evidence: a file, relative to the sample folder (the
ClinVar VCF, the haplogroup report, the PharmCAT report, the Cyrius calls),
the region (`chrom:pos`, the chromosome, or `gene SYMBOL`), and the run's
result manifest. In the HTML and Markdown these are links, relative to the
report's folder, so the report keeps working when the sample folder is moved
or copied as a whole. When the folder has been moved, the report also finds
files that manifests recorded under the old path.

The documents name the sample folder but not its full path, so the report can
be shared without the folder layout of your computer. They do contain the
sample's findings: treat them as private genetic data.

The HTML is self-contained: inline CSS, no scripts, fonts, images or other
remote resources, and `no-referrer` on its links. It renders offline in any
browser on macOS, Linux and Windows. Rendering makes no network calls; the
only remote addresses are the haplogroup tree links, which open only when you
click them.

## JSON document

`<sample>_personal_report.json` follows the `wgsextract.report/1` schema
(`src/wgsextract_cli/assets/schemas/wgsextract.report.1.schema.json`). The
same document is the `details` of the report's result manifest
(`<sample>_personal_report.manifest.json`), of `--json` output and of
`api.report.build()`.

```json
{
  "schema": "wgsextract.report/1",
  "generated": "2025-06-01T12:00:00+00:00",
  "wgsextract_version": "0.8.0",
  "sample": {"name": "HG002", "folder": "HG002", "alignment": "HG002.cram",
             "vcf": "snps.vcf.gz", "workspace": "wgsextract.sample.toml"},
  "disclaimer": "This report summarises technical results ...",
  "guide": "docs/personal-report.md",
  "confidence_reasons": {"low_depth": "read depth is below ...", "...": "..."},
  "sections": {
    "clinvar": {
      "title": "ClinVar pathogenic / likely pathogenic in ACMG SF v3.2 genes",
      "status": "ok",
      "summary": "1 call in ACMG SF v3.2 genes; 3 more elsewhere.",
      "findings": [{
        "section": "clinvar", "label": "BRCA2 Pathogenic", "value": "Pathogenic",
        "detail": "Hereditary breast ovarian cancer syndrome", "gene": "BRCA2",
        "note": null, "confidence": "high", "confidence_reasons": [],
        "confidence_basis": "wgsextract.confidence/1",
        "evidence": {"file": "clinvar_pathogenic.vcf.gz",
                     "region": "chr13:32340301",
                     "manifest": "clinvar_pathogenic.vcf.manifest.json"},
        "variant": {"chrom": "chr13", "pos": 32340301, "ref": "C", "alt": "T",
                    "genotype": "0/1", "zygosity": "het", "depth": 34,
                    "genotype_quality": 99, "alt_fraction": 0.5},
        "links": [],
        "disclaimer": "Technical result, not diagnosis."
      }],
      "missing": [], "warnings": [],
      "sources": [{"command": "vcf clinvar", "status": "ok",
                   "finished": "2025-06-01T11:00:00+00:00",
                   "manifest": "clinvar_pathogenic.vcf.manifest.json"}],
      "gene_list": {"name": "ACMG SF v3.2", "version": "3.2", "citation": "...", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC10524344/", "genes": 81},
      "build": "hg38",
      "counts": {"carried": 4, "in_gene_list": 1, "outside_gene_list": 3,
                 "high": 1, "moderate": 0, "low": 0},
      "all_calls": "clinvar_pathogenic.confidence.tsv"
    },
    "overview": {"...": "..."}, "lineage": {"...": "..."},
    "pharmacogenomics": {"...": "..."}, "microarray": {"...": "..."}
  }
}
```

Every section has `title`, `status` (`ok`, `not_run` or `failed`),
`summary`, `findings`, `missing` (`what` and the `command` to run in the
sample folder), `warnings`, `sources` and, when failed, `error`. The overview
adds `facts` and `qc`, and the microarray section adds `files` and `metrics`.

The findings table has one row per finding, with the variant's columns, the
label and its reasons, the evidence file and region, and the disclaimer. Its
columns are described in the Parquet footer like the other tables
([parquet.md](parquet.md)).

The report's result manifest records the source manifests as inputs (role
`manifest`), the four documents as outputs (`report_html`, `report_markdown`,
`report_json`, `findings_table`), the metrics `findings`,
`findings_<label>` and `section_<name>` (each section's status), and a
`section_failed` warning for each failed section.
