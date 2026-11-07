# Changelog

All notable changes to this project are tracked here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added
- Bundles can declare a local `jobServer`, and actions can carry a `serverOperation`. Those actions then run as jobs on the server, with live progress and clean cancellation, and run their command when the server isn't available ([docs/job-server.md](job-server.md)). The WGSExtract bundle runs its coverage actions on `wgsextract serve` once its bundled CLI supports `serve --ready-json`; until then they run their commands.
- Bundles can declare which commands print a JSON result document (`resultDocument`). The app then lists the files a run wrote with **Open** and **Show in Folder**, shows its warnings and key metrics, shows a refused option next to the action, and offers **Open Setup** when a tool is missing ([docs/result-documents.md](result-documents.md)). Bundle test plans can check the document with `expectedResult`. The WGSExtract bundle reads its `wgsextract` actions' documents through `--result-file`, and its wrapper scripts exit 2 when they refuse an input ([#529](https://github.com/WGSExtract/wgsextract-cli/issues/529)).

### Changed
- Bundle WGSExtract CLI `v0.6.6`: Oxford Nanopore reads are detected automatically, keep their methylation tags through alignment, and get Nanopore-tuned calling (DeepVariant ONT model, Sniffles structural variants, optional Clair3), plus CpG methylation pileups. Also adds native Linux ARM64, Y-STR genealogy panels, queryable variant tables (TSV/SQLite/Parquet), depth-aware confidence labels, `report-issue` for redacted bug reports, and easier help; DeepVariant now accepts CRAM 3.1 input and bounds its whole-genome memory use, and whole-genome realignment streams without FASTQ intermediates ([CLI release notes](https://github.com/WGSExtract/wgsextract-cli/releases/tag/v0.6.6)).
- Bundle WGSExtract CLI `v0.6.5`: fresh Windows installs work again and invalid configuration values produce clear warnings. Includes the v0.6.4 opt-in MultiQC, expanded microarray/AADR exports and targeted Y analysis, streamed eligible small paired realignment, and safer downloads, outputs and Windows cancellation ([v0.6.5 release notes](https://github.com/WGSExtract/wgsextract-cli/releases/tag/v0.6.5), [v0.6.4 features](https://github.com/WGSExtract/wgsextract-cli/releases/tag/v0.6.4)).
- Bundle WGSExtract CLI `v0.6.3`: alignment output no longer depends on thread count, BWA-MEM2 sorts in less memory, alignment checks memory before starting and falls back to BWA when MEM2 won't fit, long jobs show memory and disk estimates up front, and SNP/indel calling can optionally run per chromosome in parallel. Native Windows is now validated on full genomes ([CLI release notes](https://github.com/WGSExtract/wgsextract-cli/releases/tag/v0.6.3)).

## [0.6.2] - 2026-10-27

### Changed
- Bundle WGSExtract CLI `v0.6.2`: Windows setup now runs natively without WSL or MSYS2, with faster Windows tools. This release also adds microarray templates and Ultima support, and fixes microarray output ([CLI release notes](https://github.com/WGSExtract/wgsextract-cli/releases/tag/v0.6.2)).
- GUI for CLI is now one TypeScript app packaged with Tauri for Windows, macOS, and Linux. macOS releases ship the Tauri app (signed, notarized, and self-updating through `latest.json`) instead of the SwiftUI app and Sparkle; the macOS app is no longer named `WebUI`.
- Every bundle load validates the manifest and pages against the `docs/schema` rules (required fields, unique ids, paths that stay inside the bundle, text icons, action commands), as the Swift CLI's `bundle validate` did. `npm --prefix app run bundle:validate -- <bundle>...` checks bundles without the app, and `make lint` runs it over the examples.
- Bundles load from `.zip`, `.tar`, `.tar.gz`/`.tgz`, and gzipped `manifest.json` archives on every OS, extracted in pure Node. **Load Bundle...** (Ctrl/Cmd+O) now opens a file dialog for a bundle's `manifest.json` or a bundle archive.
- macOS releases ship separate Apple Silicon and Intel builds. Their DMGs are notarized and stapled, so Gatekeeper accepts the downloaded DMG as well as the app inside it.
- The macOS app has **File > Uninstall** again, as the SwiftUI app did: it runs the bundle's uninstall steps, removes the app's data and WebKit storage, and moves the app to the Trash. `GUI_FOR_CLI_AUTO_RUN_UNINSTALL=1` runs it unattended on macOS as on Linux.
- `make` targets (and `powershell -ExecutionPolicy Bypass -File .\make.ps1 <target>` on Windows) run through a small task runner, `tools/tasks.py`: `run`, `dev`, `tauri`, `build`, `build-tauri`, `lint`, `test`, `ci`, `package`.
- Bump the bundled WGSExtract CLI installer target to `v0.6.0`: male genomes get chrY calls and haploid chrX (they were called as female), `vcf trio` from VCFs finds de novos again, AncestryDNA kits fill in chrX/chrY/MT, DeepVariant runs on CPU from Google's Docker image, alignment uses bwa-mem2 when the reference has its index, and about 60 other fixes from three test passes.
- Bump the bundled WGSExtract CLI installer target to `v0.5.0`: alignment is ALT-aware on hs38DH and always marks duplicates, `info` shows whether an alignment was made that way (WGS Extract v4's weren't) and callers warn about it, microarray kits agree with real chips more closely (99.1% to 99.8% on AncestryDNA), `vcf trio` genotypes a trio jointly from their BAM/CRAMs, and `--gpu` aligns and calls on an NVIDIA GPU with Parabricks.
- Bump the bundled WGSExtract CLI installer target to `v0.4.3`: actions that pick a reference themselves use the one shipped beside a BAM over a library genome of another build (with hs38 installed, the mtDNA haplogroup action reported no haplogroup for the hg19 test genome), SpliceAI scores from Ensembl, and 23andMe v1/v2 microarray templates.
- Bump the bundled WGSExtract CLI installer target to `v0.4.2`: large reference downloads over 8 connections that resume, `ref status` listing all installed data, a usable PharmGKB annotation, `vcf pharmcat`, and a faster `vep --preset fast`.
- Bump the bundled WGSExtract CLI installer target to `v0.4.1`, whose Windows installer no longer needs Git (setup on a PC without Git failed installing Yleaf), runs GATK and HaploGrep on Windows, and removes MSYS2 on uninstall.
- Bump the bundled WGSExtract CLI installer target to `v0.4.0`, which adds the `--config` option the bundle now uses to keep its settings in the workspace, and fixes haplogroups for installs under paths with spaces.

### Removed
- The SwiftUI macOS app, Swift CLI, iOS and AppKit apps, Electron, NodeGUI, the TypeScript TUI, the macOS WebView shell, the Rust/Go/C/C++/.NET/Dart/Kotlin/Python/Mojo experimental renderers, and the benchmark, screenshot, and macOS updater-e2e tooling. They are preserved on the `old-experimental` branch.

## [0.3.8] - 2026-06-09

### Changed
- Bump the bundled WGSExtract CLI installer target to `v0.3.8` so new GUI builds pick up the microarray output fixes.

## [0.1.14] - 2026-06-08

### Added
- Local CI runner (`tools/ci/ci_local.py`) that mirrors the GitHub Actions pipeline; expose as `make ci` (full) and `make ci-fast` (skip iOS).
- Linux Swift Package CI job (Ubuntu, Swift 6.2 container) that runs `swift build` + `swift test` against `GUIForCLICore`.
- App-layer iOS guard: action buttons are disabled with an explanatory tooltip on iOS, where command execution is unsupported.
- Smarter pre-push hook: branches matching `release/*` run the full CI pipeline (incl. iOS build); other branches still use `--fast`.
- `LocaleMatchingTests` for `BundleSourceLoader.matchLocalizationCode` covering exact, region-stripped, Chinese script, and ordering paths.
- Windows admin setup mode for packaged WGSExtract installers.
- macOS and Windows worktree setup targets for installer lifecycle validation.
- SmartScreen install guidance on the WGSExtract download page.

### Changed
- Refactor blitz: split the four largest files into focused topic groups via Swift extensions.
  - `BundleSourceLoader` → entry + `+Manifest` + `+Localization`
  - `TerminalLogStore` → state/public API + `+Setup` + `+ProcessRunner`
  - `BundleManifestValidator` → entry + `+Pages` + `+Controls` + `+Helpers`; the monolithic `validate(_:)` body is now per-page/section/control helpers
  - `ControlRenderer` → view dispatch + `+Subviews` + `+DataSource`
- Round 2 splits: `DataSourceRunner` → entry + `+Process` (macOS); `ConfigFileBootstrapper` → entry + `+Toml` + `+Script`; `BundleSessionLoader` → entry + `+Workspace` + `+InitialState`.
- Moved reusable renderer/session helpers (`CommandRenderContext`, rendered commands, data sources, config IO, and bundle session loading) from `platform/apple/shared/app` into `GUIForCLICore`.
- CI workflow now runs the same Python script CI uses, so local `make ci` and remote CI cannot drift.
- Bumped `actions/checkout` to `@v6` (Node 24 runtime).

### Fixed
- `LocaleLinterRunner` strict mode regex no longer matches `, 0 errors`/`, 0 warnings`.
- `DataSourceRunner.signature` array literal split so Swift 6.2 type-checker doesn't time out on iOS Simulator builds.
- Cloudflare-blocked `install.tuist.io` script in CI replaced with `brew install tuist`.
- macOS CI runner now explicitly selects the newest installed Xcode so Swift 6.2 toolchain is active.
- WGSExtract macOS setup preflight behavior before bundled CLI install.
- WGSExtract installer lifecycle coverage for setup timing, app data creation, uninstall hooks, and cleanup.
