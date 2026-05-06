# PacBio Long-Read Support Handoff

## Summary

This branch adds first-class PacBio/long-read workflow support to `wgsextract` across example data discovery, alignment, DeepVariant model selection, structural variant calling, dependency discovery, documentation, and logic tests.

The implementation intentionally supports two execution paths:

- Native PacBio tooling where available, using `pbmm2` for alignment and `pbsv` for structural variants.
- Portable long-read fallback tooling on platforms where PacBio binaries are not available, using `minimap2` long-read presets and `sniffles` for structural variants.

This matters for the current development machine because it is `osx-arm64`, and conda-forge/bioconda currently do not provide usable `pbmm2` or modern `pbsv` packages for that platform. The new `pacbio` pixi environment still works on `osx-arm64` for `minimap2`, `samtools`, `pysam`, and `sniffles`.

## Branch

- Local branch: `pacbio-long-read-support`
- Base branch: `main`
- Remote: `origin https://github.com/theontho/wgsextract-cli.git`

## Main User Requirements

- Add PacBio/long-read support.
- Make PacBio genomes downloadable from the existing 1000 Genomes/example data tool by a `pacbio` tag.
- Validate real tooling and data paths where feasible, not just edit code.
- Create this root `handoff.md`.
- Commit the work, push a PR branch, and open a PR.

## Code Changes

### Alignment

File: `src/wgsextract_cli/commands/align.py`

Added long-read alignment options:

- `--platform {illumina,pacbio,hifi,clr,ont,nanopore}`
- `--preset {sr,map-pb,map-hifi,map-ont,ccs,subread}`
- `--sample SAMPLE`
- `--aligner {auto,bwa,minimap2,pbmm2}`

Added PacBio-specific alignment behavior:

- `align_pbmm2(args)` for PacBio read BAMs and FASTQ inputs.
- `.ccs.bam` inputs default to the `ccs` pbmm2 preset.
- `.subreads.bam` inputs default to the `subread` pbmm2 preset.
- FASTQ PacBio HiFi inputs can use `minimap2 -ax map-hifi`.
- ONT/nanopore inputs use `minimap2 -ax map-ont`.
- Other PacBio/long-read inputs use `minimap2 -ax map-pb` unless a more specific preset is provided.

The default Illumina behavior remains BWA-based unless long-read options or PacBio input types require another aligner.

### Variant Calling And SV Calling

File: `src/wgsextract_cli/commands/vcf.py`

Added DeepVariant PacBio model selection:

- `wgsextract vcf deepvariant --model-type PACBIO ...`
- `wgsextract vcf deepvariant --pacbio ...` as a convenience alias.
- Also exposed `HYBRID_PACBIO_ILLUMINA` in the accepted model choices.

Added structural variant caller selection:

- `wgsextract vcf sv --caller delly`
- `wgsextract vcf sv --caller pbsv`
- `wgsextract vcf sv --caller sniffles`
- `wgsextract vcf sv --pacbio`
- `wgsextract vcf sv --ccs`
- `wgsextract vcf sv --tandem-repeats tandem_repeats.bed`

PacBio SV behavior:

- `--caller pbsv` runs `pbsv discover` and `pbsv call`.
- `--caller sniffles` runs `sniffles --input ... --vcf ...`.
- `--pacbio` prefers `pbsv` when available.
- `--pacbio` falls back to `sniffles` when `pbsv` is unavailable but `sniffles` is available.

### Example Genome Catalog

File: `src/wgsextract_cli/commands/examples.py`

Added tags to example genome entries:

- New `GenomeExample.tags` field.
- `wgsextract example-genome list --tag pacbio`
- `wgsextract example-genome download --tag pacbio`

Added HGSVC2/1000 Genomes PacBio examples:

- `hgsvc2-hg00733-pacbio-hifi-bam`
- `hgsvc2-hg00732-pacbio-hifi-bam-smallest`
- `hgsvc2-hg00733-pacbio-hifi-fastq`
- `hgsvc2-na19240-pacbio-hifi-bam`

Added absolute URL support in `_source_for()` so catalog entries can point directly to public FTP/HTTPS data without being constrained to the existing 1000 Genomes base URL conventions.

Important data source:

- `https://ftp.sra.ebi.ac.uk/vol1/run/ERR386/ERR3861393/HG00732-hifi-r54329U_20190830_234003-B01.bam`

That `HG00732` movie was selected as the smallest practical HGSVC2 PacBio HiFi BAM found during investigation. A HEAD request reported `Content-Length: 3686049375`, about 3.5 GiB.

### Genome Library Handling

File: `src/wgsextract_cli/core/genome_library.py`

Changed how PacBio read BAMs are interpreted:

- `.ccs.bam` and `.subreads.bam` are treated as raw read inputs for alignment.
- These read BAMs populate the read input slot used by `align` instead of being treated as already-aligned BAMs.
- Existing alignment discovery excludes `.ccs.bam` and `.subreads.bam` so PacBio read data does not get mistaken for a finished alignment.

This is required for HGSVC2 PacBio downloads because many public PacBio data files are read BAMs, not coordinate-sorted reference alignments.

### Dependencies And Pixi Environments

Files:

- `src/wgsextract_cli/core/dependencies.py`
- `pixi.toml`
- `pixi.lock`

Added optional tool declarations:

- `pbmm2`
- `pbsv`
- `sniffles`

Mapped those tools to the `pacbio` pixi environment in dependency help logic.

Added a `pacbio` pixi environment with:

- `minimap2`
- `samtools` through existing bio tooling
- `sniffles` where supported
- `pbmm2` and `pbsv` where supported by platform
- The local `wgsextract-cli` package installed editable inside the `pacbio` environment, so `pixi run -e pacbio wgsextract ...` executes this checkout rather than a user-level executable.

Platform notes:

- `sniffles` works on the current `osx-arm64` machine.
- `pbmm2` is unavailable for `osx-arm64` in the tested package indexes.
- Modern `pbsv` is unavailable for `osx-arm64` in the tested package indexes.
- `pbmm2` and `pbsv` are included for Linux where package solving supports them.
- Older `pbmm2` is included for `osx-64` where available.

### Documentation And User Help

Files:

- `README.md`
- `src/wgsextract_cli/core/messages.py`

Updated documentation with:

- 1000 Genomes/HGSVC2 PacBio example commands.
- `example-genome list --tag pacbio` usage.
- PacBio HiFi alignment examples.
- PacBio DeepVariant examples.
- PacBio SV examples with `pbsv` and `sniffles`.

Updated help messages for VCF/SV/deepvariant workflows.

### Tests

File: `tests/test_logic.py`

Added tests for:

- PacBio catalog entries and tags.
- Tag filtering in `example-genome list` and `download`.
- Absolute source URL passthrough.
- PacBio read BAM discovery in genome libraries.
- Existing alignment discovery excluding PacBio read BAMs.
- Minimap2 long-read preset selection.
- pbmm2 alignment command construction.
- DeepVariant PACBIO model command construction.
- pbsv command construction.
- sniffles command construction.
- `--pacbio` SV fallback from pbsv to sniffles.

## Validation Completed

The following checks were run successfully after the final source changes and this handoff file were added:

```bash
pixi run ruff check --fix src/wgsextract_cli/commands/align.py src/wgsextract_cli/commands/vcf.py src/wgsextract_cli/core/dependencies.py src/wgsextract_cli/core/genome_library.py src/wgsextract_cli/commands/examples.py src/wgsextract_cli/core/messages.py tests/test_logic.py
pixi run ruff format src/wgsextract_cli/commands/align.py src/wgsextract_cli/commands/vcf.py src/wgsextract_cli/core/dependencies.py src/wgsextract_cli/core/genome_library.py src/wgsextract_cli/commands/examples.py src/wgsextract_cli/core/messages.py tests/test_logic.py
pixi run mypy src/wgsextract_cli/commands/align.py src/wgsextract_cli/commands/vcf.py src/wgsextract_cli/core/dependencies.py src/wgsextract_cli/core/genome_library.py src/wgsextract_cli/commands/examples.py src/wgsextract_cli/core/messages.py
pixi run pytest tests/test_logic.py
pixi run wgsextract example-genome list --tag pacbio
pixi run wgsextract example-genome download --tag pacbio --dry-run --target-root out/pacbio-tag-dryrun
pixi run -e pacbio which wgsextract
pixi run -e pacbio python -c "import wgsextract_cli; print(wgsextract_cli.__file__)"
pixi run -e pacbio wgsextract align --help
pixi run -e pacbio wgsextract vcf sv --help
pixi run -e pacbio wgsextract vcf deepvariant --help
pixi run -e deepvariant wgsextract vcf deepvariant --help
pixi run -e pacbio sniffles --version
pixi run -e pacbio minimap2 --version
pixi run -e pacbio samtools --version
pixi run -e pacbio python -c "import pysam; print(pysam.__version__)"
pixi run -e pacbio wgsextract deps check --tool sniffles
```

Observed outputs included:

```text
All checks passed!
Success: no issues found in 6 source files
48 passed
/Users/mac/src/wgs2/.pixi/envs/pacbio/bin/wgsextract
/Users/mac/src/wgs2/src/wgsextract_cli/__init__.py
Sniffles2, Version 2.7.5
2.30-r1287
samtools 1.21
0.23.3
/Users/mac/src/wgs2/.pixi/envs/pacbio/bin/sniffles
```

Expected platform-limited dependency checks on this `osx-arm64` host:

```bash
pixi run -e pacbio wgsextract deps check --tool pbmm2
pixi run -e pacbio wgsextract deps check --tool pbsv
```

Observed outputs:

```text
Tool not found: pbmm2
Tool not found: pbsv
```

These are expected on native `osx-arm64` because `pbmm2` and modern `pbsv` are not available for this platform in the tested package indexes. The portable PacBio path on this machine is `minimap2` plus `sniffles`.

## Real-Data Status

Real PacBio data discovery was performed, and public HGSVC2/1000 Genomes PacBio sources were added to the catalog.

The smallest identified practical HGSVC2 PacBio HiFi BAM is approximately 3.5 GiB:

```text
https://ftp.sra.ebi.ac.uk/vol1/run/ERR386/ERR3861393/HG00732-hifi-r54329U_20190830_234003-B01.bam
```

A `curl -I` check confirmed that URL returned HTTP 200 and a `Content-Length` of `3686049375`.

Full real-data alignment/SV validation was not completed in this session. The attempted remote BAM header inspection was aborted by the user:

```bash
pixi run -e pacbio samtools view -H "https://ftp.sra.ebi.ac.uk/vol1/run/ERR386/ERR3861393/HG00732-hifi-r54329U_20190830_234003-B01.bam"
```

The command produced no output before it was aborted.

## Platform Blockers

Current host facts:

- Platform: `osx-arm64`
- Docker: unavailable (`docker: command not found`)
- qemu: unavailable (`qemu-x86_64 not found`)
- Available local disk was checked and appeared sufficient for a 3.5 GiB test file.

PacBio binary availability checks:

```text
pixi search --platform osx-arm64 pbmm2
No packages found matching 'pbmm2'
```

```text
pixi run -e pacbio pbmm2 --version
pbmm2: command not found
```

```text
pixi run -e pacbio pbsv --version
pbsv: command not found
```

```text
pixi run -e pacbio wgsextract deps check --tool pbmm2
Tool not found: pbmm2
```

```text
pixi run -e pacbio wgsextract deps check --tool pbsv
Tool not found: pbsv
```

Because of these constraints, the implemented and validated native path on this machine is `minimap2 map-hifi` plus `sniffles`. The `pbmm2` plus `pbsv` path should be validated on a `linux-64` runner or host.

## Recommended Follow-Up Validation

### Small Real-Data Extraction

Use the smallest catalogued PacBio example first:

```bash
pixi run wgsextract example-genome download hgsvc2-hg00732-pacbio-hifi-bam-smallest --target-root out/pacbio-real
```

Set `genome_library = "/absolute/path/to/out/pacbio-real"` in `config.toml`, or pass `--input` and `--outdir` explicitly instead of using `--genome`. Then create a small extracted read set from that BAM into `out/` or `tmp/` only. Keep all logs and temporary outputs under `out/` or `tmp/` per repository instructions.

Suggested native `osx-arm64` path:

```bash
pixi run -e pacbio wgsextract --genome test-1000genomes/hgsvc2-hg00732-pacbio-hifi-bam-smallest align --platform hifi --aligner minimap2 --ref <reference.fa> --sample HG00732
pixi run -e pacbio wgsextract vcf sv --caller sniffles --input <aligned.bam> --ref <reference.fa> --outdir out/pacbio-real/hg00732-small
```

Exact paths should be adjusted to the genome-library layout produced by the download command.

### Full Real-Data Run

After the small extraction succeeds, run a full alignment/SV pass on the 3.5 GiB HG00732 HiFi BAM.

Expected native `osx-arm64` path:

```bash
pixi run -e pacbio wgsextract --genome test-1000genomes/hgsvc2-hg00732-pacbio-hifi-bam-smallest align --platform hifi --aligner minimap2 --ref <reference.fa> --sample HG00732
pixi run -e pacbio wgsextract vcf sv --pacbio --input <aligned.bam> --ref <reference.fa> --outdir out/pacbio-real/hg00732-full
```

On `osx-arm64`, `--pacbio` should choose `sniffles` because `pbsv` is unavailable.

Expected Linux path:

```bash
pixi run -e pacbio wgsextract --genome test-1000genomes/hgsvc2-hg00732-pacbio-hifi-bam-smallest align --platform hifi --aligner pbmm2 --ref <reference.fa> --sample HG00732
pixi run -e pacbio wgsextract vcf sv --caller pbsv --ccs --input <aligned.bam> --ref <reference.fa> --outdir out/pacbio-real/hg00732-pbsv
```

### DeepVariant PacBio Run

Validate DeepVariant PacBio model selection once an aligned PacBio BAM exists and Docker/DeepVariant execution is available:

```bash
pixi run -e deepvariant wgsextract vcf deepvariant --pacbio --input <aligned.bam> --ref <reference.fa> --outdir out/pacbio-real/hg00732-deepvariant
```

## Risks And Review Notes

- `pixi.lock` changed substantially because the new `pacbio` environment and platform-specific dependencies expanded the solved package set.
- `pbmm2` and `pbsv` command construction is covered by tests, but actual execution was not validated on this `osx-arm64` host due package availability.
- `sniffles` execution was validated only for tool availability and CLI command construction, not against a full real PacBio BAM in this session.
- PacBio read BAM classification treats `.ccs.bam` and `.subreads.bam` as raw reads, and also respects `fastq_r1` entries in `genome-config.toml` for plain `.bam` PacBio examples.
- If future public PacBio read BAMs use other suffixes and are not catalogued with `fastq_r1`, the genome-library detection may need to be extended.
- The example catalog uses public external FTP/HTTPS URLs; availability and transfer speed depend on external services.

## Files Changed

- `README.md`
- `pixi.toml`
- `pixi.lock`
- `src/wgsextract_cli/commands/align.py`
- `src/wgsextract_cli/commands/examples.py`
- `src/wgsextract_cli/commands/vcf.py`
- `src/wgsextract_cli/core/dependencies.py`
- `src/wgsextract_cli/core/genome_library.py`
- `src/wgsextract_cli/core/messages.py`
- `tests/test_logic.py`
- `handoff.md`

## Suggested PR Summary

```markdown
## Summary
- Add PacBio/long-read alignment support with pbmm2 and minimap2 presets.
- Add PacBio DeepVariant model selection plus pbsv/sniffles SV calling.
- Add HGSVC2/1000 Genomes PacBio examples with tag-based downloads and tests.

## Testing
- pixi run ruff check --fix ...
- pixi run ruff format ...
- pixi run mypy ...
- pixi run pytest tests/test_logic.py
- pixi run wgsextract example-genome list --tag pacbio
- pixi run wgsextract example-genome download --tag pacbio --dry-run --target-root out/pacbio-tag-dryrun
- pixi run -e pacbio wgsextract align --help
- pixi run -e pacbio wgsextract vcf sv --help
- pixi run -e pacbio wgsextract vcf deepvariant --help
```
