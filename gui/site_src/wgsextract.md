---
output: 'wgsextract.html'
title: 'WGSExtract and GUI for CLI'
description: 'WGSExtract v0.6.6 workflows and how the first real app shaped GUI for CLI.'
eyebrow: 'First real app'
heading: 'WGSExtract made GUI for CLI prove itself.'
lede: 'The WGSExtract bundle is the first real application built through GUI for CLI. It pushed the project beyond demo controls into setup, command execution, data state, localization, and packaged releases.'
actions: 'Architecture|architecture.html; Documentation|docs.html'
footer_title: 'WGSExtract'
footer_text: 'A real workflow drove the generic app shell.'
---

::: section
::: wrap
::: section-head
## Why this bundle mattered

WGSExtract gave GUI for CLI a real target: a genomics workflow with long-running tools, user-selected files, setup state, reference data, and packaging expectations.
:::

::: grid three
::: card
{{ kicker: Setup }}
### More than a launch button
The bundle needs setup steps, environment checks, external scripts, tool paths, and recoverable state before actions are safe to run.
:::

::: card
{{ kicker: Commands }}
### Long-running process UX
Genome commands need terminal output, cancellation, exit code handling, path arguments, generated command previews, and clear disabled states.
:::

::: card
{{ kicker: Data }}
### Dynamic rows and config
WGSExtract exercises data sources, state-dependent actions, persisted settings, localization, icons, and workspace files.
:::
:::
:::
:::

::: section
::: wrap
## What v0.6.6 exposes

The desktop app bundles **WGSExtract CLI v0.6.6**. Nanopore reads work through the actions already in the app with no settings to change, and new opt-in controls cover the rest. They start blank or unchecked, so automatic detection and `config.toml` defaults keep applying.

### Oxford Nanopore in existing actions

Nanopore (ONT) reads are detected from `PL:ONT` read groups or UUID read names.

- **Align and Realign:** use minimap2's `map-ont` preset and keep MM/ML methylation tags through alignment and sorting. Realign refuses its keep-FASTQ option for ONT input rather than lose those tags. FASTQs renamed without platform metadata, such as SRA downloads, are treated as Illumina, and the app has no platform override.
- **DeepVariant:** selects the ONT_R104 model. It supports R10.4.1 chemistry only and needs x86-64, so ONT calling is refused on Apple Silicon and ARM64. DeepVariant itself is still a separate install.
- **SV Call:** runs Sniffles2 instead of Delly. App setup does not install Sniffles; install it separately before running this action on ONT data.

### Other improvements in existing actions

- **DeepVariant:** CRAM 3.1 input is converted to a temporary CRAM 3.0 copy, and whole-genome postprocessing memory is estimated and capped.
- **Realign:** paired reads stream through collation at whole-genome scale, and single-end input is refused before extraction instead of being dropped.
- **BAM Info:** sex is inferred when chrX or chrY is missing, and depth binning is faster.
- **Microarray:** coordinate projections and catalogues are cached for faster runs.

### New opt-in controls

- **DeepVariant:** a model dropdown that defaults to automatic. Its ONT_R104 choice supports R10.4.1 chemistry only and needs x86-64.
- **SV Call:** a long-read mode (Nanopore or any long-read data) and an optional tandem-repeat BED. The caller stays automatic, and pbsv is not offered for Nanopore.
- **Clair3:** an optional Nanopore caller with an optional model override, qualified on Linux x86-64 only. It is hidden on Windows, and you install it separately.
- **Methylation Pileup:** a bedMethyl table and summary from MM/ML-tagged reads, with optional context, 5hmC and strand overrides. It is hidden on Windows, and you install modkit separately.
- **Ancestry:** Y-STR repeat counts with FamilyTreeDNA, PowerPlex Y23 and Yfiler Plus panels.
- **Annotate:** a variant table as TSV plus an SQLite database to query.
- **Settings:** preview a redacted `report-issue` bug report. Nothing is uploaded, and the browser does not open.

### CLI only for now

Parquet table output, vendor chip detection, JSON result manifests, and the alignment platform override are not in the app, so run them from the CLI. Native Linux ARM64 DeepVariant is CLI only too, because Linux ARM64 desktop installers are not published.

### Controls from earlier releases

These came with [GUI #171](https://github.com/WGSExtract/gui-for-cli/pull/171). They start unchecked or blank, inheriting CLI/config defaults rather than forcing them off.

- **FASTQ:** aggregate existing FastQC reports into offline MultiQC HTML, with file and directory pickers and explicit report replacement. MultiQC must already be available; this action neither installs it nor runs FastQC.
- **Extract and Library:** optional Poz Y-BAM regions (GRCh37/38), unmapped FASTQs, and allele-annotated yBrowse Y-SNP calling on matching indexed GRCh38. Install the pinned yBrowse resource explicitly from Library first.
- **Microarray:** marker-only MyHeritage GSA and Ancestry layouts; AADR 1240K, HO, union, and opt-in v66.p1 2M PLINK/EIGENSTRAT research exports; quality/depth filtering, strand masking, historical rsIDs, no-call/reason reports, and a sequence-defined I/D manifest bound to one selected consumer format.
- **BAM Info:** streaming per-chromosome depth bins and optional retention of FASTQs extracted during realignment.
- **VCF:** opt-in Ultima flow-mode GATK scattering. It can change genotypes and annotations and needs 6 GiB RAM per worker; unscattered calling stays the default.
- **Ancestry and Settings:** optional lineage-tree links, persistent logs, an existing temporary-workload volume, atomic main-output publication, and best-effort keep-awake.

AADR 2M is excluded from `all`: explicitly install its pinned metadata from Library first. It supports GRCh37/38 only; GRCh38 also needs an already-installed forward chain. It does not support CHM13/T2T, and extraction never downloads missing metadata or chains. Use non-identifying AADR sample labels.

CLI setup is native on Windows, without WSL or MSYS2, and GUI settings save real TOML booleans and numbers, including `false`. Earlier CLI releases also fixed fresh Windows installation, added warnings for invalid configuration values, and improved alignment reproducibility, memory preflight, and native Windows full-genome validation. Tool and platform requirements still vary by workflow.

[Download v0.6.6](index.html), read the [CLI v0.6.6 release notes](https://github.com/WGSExtract/wgsextract-cli/releases/tag/v0.6.6) and [ONT limitations](https://github.com/WGSExtract/wgsextract-cli/blob/v0.6.6/docs/ont.md), or see the [workflow details and prerequisites](https://github.com/WGSExtract/wgsextract-cli/blob/main/gui/docs/wgsextract-post-v063.md).
:::
:::

::: section
::: wrap
## What it forced into the platform

- bundle loading from folders and archives;
- `manifest.json` and page schema validation;
- setup dry-runs and setup execution;
- dynamic page controls and action state;
- terminal output panes and process lifecycle handling;
- config storage in platform-standard Application Support paths;
- localized strings and semantic icon maps;
- bundle-specific packaging and app identity.

Those requirements are generic. The WGSExtract app is concrete, but the runtime behavior belongs to GUI for CLI so another CLI tool can reuse the same shell.
:::
:::

::: section
::: wrap
## Why it shaped the frontend decision

A toy bundle can make any renderer look good. WGSExtract made the comparison harder: the frontend had to carry real commands, real setup, and real release packaging on Windows, macOS, and Linux. One TypeScript app packaged with Tauri was the surface that carried all of it with a single codebase, so it became the only frontend.
:::
:::
