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

With `--input`, only that file's runs are reported: runs on it, on files made
from it (its `vcf snp` VCF, say) and the runs that made it. In a sample
workspace or genome folder, which holds one person, runs on files made from no
other alignment (a VCF from a lab, say) are included too. Without `--input`, a
plain folder holding results of several samples is an error (exit 2) naming
them; choose one with `--input`. The report reads only manifests and the
workspace, so it needs no samtools or other analysis tools, even for a BAM or
CRAM `--input`.

> **Technical result, not diagnosis.** The report describes what software
> found in this sequencing run. It is not a diagnosis or medical advice.
> Confirm any finding with an accredited clinical laboratory before acting on
> it.

## Sections

| Section | Source runs | Findings |
| --- | --- | --- |
| Sample overview | workspace facts (sex, platform, build, coverage, depth by target, read length, each with its method and confidence); `info --detailed` (QC metrics, plus sex, coverage and read length with confidence "not recorded" when the folder has no workspace) | none: facts and QC tables |
| Lineage | `lineage y-haplogroup`, `lineage mt-haplogroup` | the Y and mtDNA haplogroups, with tree links (YFull, FTDNA). Y-DNA is not asked for when the recorded sex is Female. |
| Pharmacogenomics | `vcf pharmcat`, `typing cyp2d6` (Cyrius), `typing hla` (T1K) | one per PharmCAT gene (diplotype and phenotype); the CYP2D6 diplotype; the HLA-A and HLA-B alleles |
| ClinVar | `vcf clinvar` | each carried ClinVar pathogenic or likely-pathogenic call in a gene of the gene list (default ACMG SF v3.3), and per gene the calls by zygosity, with recessive genes marked carrier or (possibly) biallelic |
| Microarray export | `microarray` | none: the files produced, with sites, called sites and no-call rate |

Warnings the source runs recorded (low coverage, high no-call rates, ...) are
shown with their section.

### ClinVar gene list

ClinVar hits are listed only for the genes of one gene list, by default the 84
genes of the ACMG secondary-findings list, version 3.3 (Lee K et al., *Genet
Med* 2025;27(8):101454, PMID 40568962). The genes, categories, inheritance
and notes were checked against the paper's
[Table 1](https://pmc.ncbi.nlm.nih.gov/articles/PMC12318660/). v3.3 adds
ABCD1, CYP27A1 and PLN to v3.2 and moves HFE to inborn errors of metabolism.

Pick another list with `--gene-list` or `report_gene_list` in config.toml:

- **A built-in list**: `acmg-sf-3.3` (the default) or `acmg-sf-3.2`. Built-in
  lists are assets of the GitHub release `report-gene-lists-v3`, pinned by
  SHA-256 in `src/wgsextract_cli/assets/report/gene_lists.json`. The report
  downloads one the first time it has ClinVar calls to place, into
  `report/gene_lists/` of the reference library, and checks it against the
  pinned SHA-256 whenever it reads it. Offline, without a downloaded list,
  the section warns (`gene_list_unavailable`) and places no calls; the other
  sections are unaffected.
- **A TSV file** with a `gene` column, and optionally `category`,
  `inheritance` (`AD`, `AR`, `SD`, `XL`; see below), `note`, and
  spans (1-based, inclusive): `hg19_chrom`, `hg19_start`, `hg19_end` and the
  `hg38_` columns, or `chrom`, `start` and `end` in the VCF's build. Lines
  starting with `#` are comments. The built-in lists use this format.
- **A BED file** (`.bed` or `.bed.gz`) with the gene symbol in column 4, in
  the VCF's build. A gene keeps every interval, on any chromosome.

A call is placed in a gene by ClinVar's own gene symbols (`GENEINFO`, which
`vcf clinvar` carries over when the ClinVar file has it). For calls annotated
without `GENEINFO`, it is placed by the gene spans, in the build of the VCF
`vcf clinvar` read. A list of symbols alone therefore needs `GENEINFO`; calls
that neither can place are counted as `unplaced`, with a
`gene_annotation_missing` warning (or `build_unknown` when the VCF's build is
unknown). A variant ClinVar places in several listed genes is one finding,
named after all of them (`GENE1/GENE2`). The section also counts the P/LP
calls outside the list and links the full table of every carried call
(`clinvar_pathogenic.confidence.tsv`). A user's file has no version, citation
or URL; those fields are empty strings in the JSON.

The built-in spans are each gene's MANE Select transcript in NCBI's annotation
release GCF_000001405.40-RS_2025_08 (GRCh38.p14) and its RefSeq Select
transcript in release 105.20220307 (GRCh37.p13); APC's also covers promoter 1B
(NM_001127511). One transcript keeps readthrough and alternative models from
reaching neighbouring genes (MSH2 into KCNK12, BRCA1 into NBR2).
`scripts/build_report_gene_lists.py` builds the lists, records where they
differ from UCSC refGene, and stops on a difference over 25 kb unless the
gene is a reviewed exception (recorded in the release's `build_info.json`).

ACMG reports some genes only in particular cases. For MUTYH, CASQ2, TRDN, BTD,
GAA, ATP7B, RPE65 and CYP27A1 only two P/LP variants (biallelic) count, for
ABCD1 not a single heterozygous one, and for HFE only p.C282Y homozygotes. TTN
counts only truncating variants, RYR1 only malignant-hyperthermia variants,
and LMNA not missense variants never seen with cardiac disease. Those findings
carry this as a note.

The section also counts each listed gene's P/LP calls by zygosity
(`heterozygous`, `homozygous`, `hemizygous` for a haploid genotype, `partial`
for a genotype whose other allele was not called, such as `1/.`), with the
weakest confidence label of those calls. For a gene the list marks as
autosomal recessive (`inheritance` `AR`; the built-in lists take it from Table
1) it gives a status:

- **biallelic**: a homozygous call, or calls of two ALTs of one multi-allelic
  call such as `1/2` (one on each copy; `vcf clinvar` keeps the split record
  in `OLD_MULTIALLELIC`);
- **possible biallelic**: two or more heterozygous calls, whose phase is
  unknown, so they may be on the same copy (cis) rather than one on each
  (trans); or a hemizygous or partly called genotype, which doesn't show the
  other copy;
- **carrier**: one heterozygous call.

So an HFE p.C282Y homozygote shows as biallelic, and two heterozygous ATP7B
calls as possible biallelic. The summary names each status, with the calls'
confidence when it is not high. The report does not work out phase, variant
consequence or which HFE variant a call is, and X-linked genes get counts but
no status. If the run's confidence table is missing, its manifest lists only
the first 1,000 calls; the section then warns (`calls_truncated`) and gives
only biallelic statuses, which more calls can't change.

## Confidence labels

Every finding has a label, `high`, `moderate`, `low` or `not_assessed`, with
reason codes. The JSON document explains each code in `confidence_reasons`.

- **ClinVar**: the call's own label from `vcf clinvar`
  ([variant-confidence.md](variant-confidence.md)): depth, genotype quality,
  allele balance, mapping quality and strand bias.
- **PharmCAT**: the weakest label among the gene's labelled calls in the
  run's confidence table. A gene with no labelled calls (a reference `*1/*1`
  rests on coverage alone) is `not_assessed`.
- **HLA-A and HLA-B (T1K)**: `low` when `typing hla` made no confident call;
  `high` when T1K's quality is above 0 for each allele (its own threshold) and
  the names it tied agree; `moderate` when the T1K genotypes file is missing;
  `not_assessed` (with an `evidence_mismatch` warning) when the file no longer
  gives the call the run recorded. The detail gives the qualities and the
  IPD-IMGT/HLA release.
- **CYP2D6 (Cyrius)**: `low` when Cyrius made no call or its filter did not
  pass; otherwise the median depth against the `confidence_min_depth` and
  `confidence_high_depth` settings (`low` below the minimum, `moderate`
  below the high threshold, `high` above it).
- **Haplogroups**: the caller's quality score (Yleaf QC-score, Haplogrep
  3 quality): `high` from 0.9, `moderate` from 0.7, `low` below 0.7. A run that
  warned of low coverage is `low`.

## Evidence and privacy

Each finding names its evidence: a file, relative to the sample folder (the
ClinVar VCF, the haplogroup report, the PharmCAT report, the Cyrius calls, the
T1K genotypes), the region (`chrom:pos`, the chromosome, or `gene SYMBOL`), and the run's
result manifest. In the HTML and Markdown these are links, relative to the
report's folder, so the report keeps working when the sample folder is moved
or copied as a whole. When the folder has been moved or copied, the report
uses the files in its own folder rather than the paths the manifests recorded.
Evidence on another drive (`--results` on Windows) has no relative path, so it
is named, and linked, by its absolute path.

The documents name the sample folder but not its full path, so the report can
be shared without the folder layout of your computer. Paths in the runs'
warnings and errors are made relative to the sample folder, or cut to the file
name when outside it. They do contain the
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
      "title": "ClinVar pathogenic / likely pathogenic in ACMG SF v3.3 genes",
      "status": "ok",
      "summary": "1 call in ACMG SF v3.3 genes; 3 more elsewhere.",
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
      "gene_list": {"id": "acmg-sf-3.3", "name": "ACMG SF v3.3", "version": "3.3", "citation": "...", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC12318660/", "genes": 84},
      "build": "hg38",
      "counts": {"carried": 4, "in_gene_list": 1, "outside_gene_list": 3,
                 "unplaced": 0, "high": 1, "moderate": 0, "low": 0},
      "genes": [{"gene": "BRCA2", "inheritance": "AD", "heterozygous": 1,
                 "homozygous": 0, "hemizygous": 0, "partial": 0,
                 "confidence": "high", "status": null, "status_text": ""}],
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
adds `facts` and `qc`, ClinVar adds `gene_list`, `build`, `counts`, `genes`
and `all_calls`, and the microarray section adds `files` and `metrics`.

The findings table has one row per finding, with the variant's columns, the
label and its reasons, the evidence file and region, and the disclaimer. Its
columns are described in the Parquet footer like the other tables
([parquet.md](parquet.md)).

The report's result manifest records the source manifests as inputs (role
`manifest`), the four documents as outputs (`report_html`, `report_markdown`,
`report_json`, `findings_table`), the metrics `findings`,
`findings_<label>` and `section_<name>` (each section's status), and a
`section_failed` warning for each failed section.
