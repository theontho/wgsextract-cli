# WGSExtract: features after CLI v0.6.3

These controls require CLI v0.6.4 or newer. This feature branch is rebased onto
the separate v0.6.5 bundle-bump PR, which also fixes fresh Windows installation
and adds clear warnings for invalid configuration values. Root approves merges
and app release tags; neither PR should be merged without that approval.

Existing actions and selected microarray formats are unchanged. New selections
are unchecked or blank: unchecked means inherit CLI/config defaults, not force
false. All operations remain explicit user actions.

- **FASTQ:** aggregate existing FastQC reports into offline MultiQC HTML.
  The CLI validates MultiQC availability; this does not install tools or run
  FastQC automatically. Replacing a report requires explicit selection.
- **Extract:** optional Poz Y-BAM regions (GRCh37/38), installed allele-annotated
  yBrowse Y-SNP calling (matching indexed GRCh38 only), and unmapped FASTQs.
  Install the pinned GRCh38 yBrowse resource explicitly from Library first.
- **Microarray:** marker-only MyHeritage GSA and Ancestry layouts; AADR
  1240K/HO/union and opt-in v66.p1 2M PLINK/EIGENSTRAT research exports;
  optional GQ/DP filtering,
  strand masking, historical rsIDs, absent/filtered no-calls and reason reports.
  AADR sample labels must be non-identifying; sex metadata does not change calls.
  One sequence-defined I/D manifest can be bound to a selected consumer format;
  repeated bindings remain advanced CLI use. CHM13 uses the supported pinned
  reference selected in the existing dropdown, not an invented mode flag.
  The 2M panel is excluded from `all` and requires explicitly installed metadata
  from Library's **Install AADR 2M metadata** action. It supports GRCh37/38 only;
  GRCh38 needs an already-installed forward chain, and CHM13/T2T is rejected.
  Metadata is checksum-pinned CC0 with Reich Lab/Harvard Dataverse attribution.
  Extraction never installs it or downloads a missing chain.
- **BAM Info:** a separate streaming per-chromosome depth-bin action and optional
  retention of FASTQs extracted during realignment.
- **VCF:** opt-in Ultima flow-mode GATK scattering; it can change genotypes and
  annotations and requires 6 GiB RAM per worker. Default stays unscattered.
- **Ancestry:** optional printed lineage tree links.
- **Settings:** opt-in persistent logs, an existing temporary-workload volume,
  atomic main-output publication and best-effort keep-awake.

Settings toggles save as native TOML booleans, including `false`; values from
form inputs are converted according to the setting's kind/default. Numeric
defaults declare numeric settings; path/text defaults remain strings even when
their text is `false` or `123`. Bootstrap merging preserves scalar TOML types.
All existing WGSExtract non-toggle settings are strings; the unrelated Annotate
toggle is stored in GUI JSON state, not the CLI TOML file.
Wrong-type saved scalars report a warning and use the setting's declared
default, without preventing bundle/Settings loading or rewriting the file
on read. Quoted booleans/numbers are not treated as native TOML scalars.
Save-time validation remains strict; form strings are converted to the
declared setting type before publication.

## Release and validation

The hourly CLI release watcher opens a separate `cli-<tag>` bundle-bump PR and
dispatches CI, secrets scanning and Windows installer checks. It does not update
this feature branch. After v0.6.4, either apply `scripts/bump-wgsextract-cli.py`
here and retire the duplicate bump PR, or land the separate bump first and
rebase. Root approves all merges.

Run `make lint` and `make test`. Renderer tests check unchanged defaults and
opt-in argument routing; POSIX wrapper tests check argument boundaries and,
when a CLI interpreter is available, actual argparse compatibility.
CI conformance rejects post-v0.6.3 controls/actions/formats while the bundled pin
is older than v0.6.4. The genuine v0.6.5 pin satisfies this gate; never fake the
pin or disable the test to make this PR mergeable.

After the bundle bump, run the existing hg19 mini baseline plan, then
`post-v063-mini-opt-in.json` against the same workspace. MultiQC must already be
available. This adds exact depth bins, unmapped FASTQs, offline aggregation and
explicit report replacement. Then run the existing hs38DH/Ladan full-genome
microarray plan unchanged for comparison. On matching real references/data,
pass the full plan an explicit `--input ref_fasta=<matching hs38DH FASTA>`
selection if the reference dropdown has not yet been selected. On real data,
exercise the new layouts, AADR, quality/strand policies, manifest I/D, CHM13,
Poz/yBrowse and Ultima scattering. They are not interchangeable with the scaled
mini reference. Verify Windows cancel terminates the tool tree.
The mini plan scans the bootstrapped `output` directory for FastQC reports;
its per-action output field does not change FastQC's inherited CLI config.
MultiQC offers separate file and directory pickers/actions.
For the 2M panel, explicitly install metadata in Library first, then run
`aadr-2m-opt-in.json` with command-line `--input bam_path=<real BAM/CRAM/VCF>`
and `--input ref_fasta=<matching GRCh37/38 FASTA>` overrides. This plan is not
valid on the scaled mini reference. Check its six PLINK/EIGENSTRAT files and
the CLI's pinned panel/provenance metadata; the original full-genome baseline
plan is unchanged.

ARM64 GUI packaging is a separate follow-up: neither Linux ARM64 nor Windows
ARM64 is currently built in distribution/installer CI. Native Node sidecars are
already supported, but native runner matrices, installer coverage and
architecture-specific updater-feed mappings are needed. CLI setup downloads
the pinned source and delegates architecture selection to upstream installers;
it does not select a prebuilt per-architecture CLI asset. Wait for CLI ARM64
support and a release, then verify native Pixi/tool selection (including Windows
MSYS2/emulation assumptions) from native ARM64 GUI installers.
