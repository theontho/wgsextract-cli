# Native Yleaf

Derived from [Yleaf](https://github.com/genid/Yleaf) v4.1.6 (GPL-3.0-or-later).
`provenance.json` is the authoritative upstream tag/commit. See the repository
`NOTICE` for authorship and the Ralf et al. (2018) citation
([doi:10.1093/molbev/msy032](https://doi.org/10.1093/molbev/msy032)).

## Interface and behavior

`analysis.analyze()` accepts single-sample VCF/BCF, indexed BAM or CRAM, a build
(`hg19`, `hg38`, `t2t`), tree (`yfull`, `yfull_v10`, `ftdna`, `isogg`) and
optional reference FASTA. The CLI exposes the same settings through
`lineage y-haplogroup`. Align FASTQ with `wgsextract align` first; the old
`--yleaf-path` and `--extra-args` are replaced by native typed flags.

The adapter calls only the existing samtools/bcftools tools, not Yleaf.
No tools or full reference genomes are automatically installed/downloaded.
CRAM requires the matching reference. Variant-only VCF inference also needs
the matching reference (`--ref`, or the configured reference library).
It treats missing variants as matching the reference, exactly as upstream;
this is **not coverage evidence**. For targeted panels, use
`--no-ref-inference` so absent sites cannot become inferred genotypes.
As upstream, GT-only VCFs use their first allele and bypass read thresholds;
AD uses the reference versus the highest-depth alternate (not the sum of AD).

Outputs in `<outdir>/yleaf/`:

* `hg_prediction.hg`: upstream-compatible TSV, including QC1/QC2/QC3/counts.
* `<sample>/<sample>.out`, `.fmf`, `.info`: observed/inferred and filtered markers.
* `run.json`: upstream code/data tag, commit, build, tree and QC settings.

Calls and QC values preserve v4.1.6 behavior, including recurrent-site and
approximate-placement exclusions, 60% node consensus, deepest passing node,
FTDNA's missing-ancestor policy and v10's use of the v14 QC1 expectation tables.
Marker names in the final display are sorted (upstream uses unordered sets);
marker-row and filtered-row ordering/number formatting may differ. No user
directory is recursively deleted. Only the current prediction file is replaced.

## Data cache

`data_manifest.json` pins archive URLs, sizes and SHA-256 digests, and the digest
of every extracted file. Assets are hosted on this repository's dedicated
`yleaf-data-v4.1.6` release, not in the wheel or GUI bundle. Only the selected
build and prediction tables are lazily downloaded into
`<reference_library>/yleaf/v4.1.6/`. Downloads use the reference-library
resumable, checksum-verifying downloader. Archives are staged under that
library and only manifest-listed regular files are accepted. A lock serializes
publication; cached files are reverified, with corrupt/missing tables repaired.
A valid cache works offline; an uncached offline run reports what is missing.

## Upstream updates

1. Clone/fetch upstream **outside the package**, e.g. into `scratch/Yleaf`, and
   fetch the new upstream tag. Keep the old base commit available locally.
2. Run (capture review output under `out/`):

   ```sh
   pixi run python scripts/sync_yleaf.py --upstream scratch/Yleaf \
     --tag vNEXT --output out/yleaf-native/vNEXT
   ```

   The script diffs the prediction, tree, extraction/constants source against
   the recorded base and reports data changes. It creates deterministic gzip
   tarballs per build and prediction directory, a source-pinned file/archive
   manifest, `SHA256SUMS`, and the upstream GPL license.
3. Review/port algorithm changes, preserving original attribution. Run focused
   tests, upstream parity (below), and real male WGS parity for each build.
   Only then repeat with `--write-manifest` to update package data pins and
   the single authoritative provenance record. Do not silently advance a
   provenance tag without reviewing the code diff.
4. Create a **draft**, non-latest data release:

   ```sh
   gh release create yleaf-data-vNEXT out/yleaf-native/vNEXT/* \
     --draft --target main --title "Yleaf data vNEXT" \
     --notes "Unmodified Yleaf tables; upstream source/tag/commit and GPL license in assets."
   ```

   Check uploaded draft assets with `gh release download` and `SHA256SUMS`.
   Draft browser-download URLs are not public: publish with
   `gh release edit yleaf-data-vNEXT --draft=false --latest=false`, then test
   `ensure_data()` with an empty isolated reference library against the public
   URLs for **each** build. Leave the data release non-latest so application
   installers never mistake it for a software release.
5. Commit the reviewed code, provenance and manifest together. Publish parity
   evidence without local input names or paths.

## Parity checks

The normal test suite uses tiny synthetic tables without network access.
Additional opt-in comparisons (no download on absent inputs):

```sh
pixi run pytest -c pyproject.toml tests/test_yleaf_native.py --basetemp=out/yleaf-native/pytest \
  --yleaf-upstream scratch/Yleaf
pixi run pytest -c pyproject.toml tests/test_yleaf_native.py --basetemp=out/yleaf-native/pytest \
  --yleaf-parity-output out/yleaf-native/upstream/sample \
  --yleaf-library out/yleaf-native/library/yleaf/v4.1.6
# Also rerun extraction (same original input/build and upstream QC defaults):
pixi run pytest -c pyproject.toml tests/test_yleaf_native.py --basetemp=out/yleaf-native/pytest \
  --yleaf-parity-output out/yleaf-native/upstream/sample \
  --yleaf-library out/yleaf-native/library/yleaf/v4.1.6 \
  --yleaf-parity-input out/yleaf-native/sample.bam --yleaf-parity-build hg38
```

The first compares scoring with the tagged upstream implementation. The second
replays an upstream marker table and compares haplogroup, all QC values and
QC-relevant marker counts. Supplying the original input/build additionally
compares full extraction `.out`/`.fmf` rows normalized for numeric serialization
and order. Add `--yleaf-parity-ref` for CRAM or VCF reference inference; without
it, VCF parity disables reference inference. The unordered, truncated final
marker display is excluded; complete marker tables are compared instead.
