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
An incompatible detected FASTA/build combination is rejected, including when
`--build` overrides input metadata; pass a matching `--ref` explicitly.
It treats missing variants as matching the reference, exactly as upstream;
this is **not coverage evidence**. For targeted panels, use
`--no-ref-inference` so absent sites cannot become inferred genotypes.
As upstream, GT-only VCFs use their first allele and bypass read thresholds;
AD uses the reference versus the highest-depth alternate (not the sum of AD).

Outputs in `<outdir>/yleaf/`:

* `hg_prediction.hg`: upstream-compatible TSV, including QC1/QC2/QC3/counts.
* `<sample>/<sample>.out`, `.fmf`, `.info`: observed/inferred and filtered markers.
* `run.json`: upstream code/data tag, commit, build, the tree's id, name,
  version, date, license and source, ancient-panel/QC settings, and the selected
  marker panel's identifier and SHA-256 (including custom panels).

Calls and QC values preserve v4.1.6 behavior, including recurrent-site and
approximate-placement exclusions, 60% node consensus, deepest passing node,
FTDNA's missing-ancestor policy and v10's use of the v14 QC1 expectation tables.
Marker names in the final display are sorted (upstream uses unordered sets);
marker-row and filtered-row ordering/number formatting may differ. No user
directory is recursively deleted. Only the current prediction file is replaced.

## Trees

`trees.json` records every `--tree`: name, version, release date, license and
source. The CLI prints the tree's name and version with each result; JSON
results carry them in `details["tree"]`.

| `--tree` | Tree | Version | Markers from |
|---|---|---|---|
| `yfull` (default) | YFull YTree, CC-BY-4.0 | see `trees.json` | tree pack built by `scripts/build_yfull_tree.py` |
| `yfull_v10` | YFull YTree | 10.01.0 (2022-02-25) | Yleaf v4.1.6 data |
| `ftdna` | FamilyTreeDNA Y-DNA tree | 2026-03-26 | Yleaf v4.1.6 data |
| `isogg` | ISOGG Y-DNA tree | 2020-07 SNP table, 2023-08 index | Yleaf v4.1.6 data |

The `yfull` tree is a *tree pack*: Yleaf's `tree.json` plus hg19, hg38 and T2T
marker tables (`new_positions.txt`, `new_positions_ancient.txt`), built
reproducibly from a YFull [YTree](https://github.com/YFullTeam/YTree) release
and a [YBrowse](https://ybrowse.org) SNP snapshot. It is pinned by SHA-256 in
`trees.json`, hosted on the `yleaf-tree-yfull-v<version>` release and verified
into `<reference_library>/yleaf/trees/yfull/<version>/` on first use. The other
trees and Yleaf's QC1 backbone tables still come from the Yleaf data cache.

Each SNP group on a branch (`/`-separated aliases) becomes one marker when its
aliases name exactly one GRCh38 chrY single-base substitution in YBrowse.
hg19 and T2T positions are lifted with Yleaf's UCSC chains (alleles are
complemented on reverse-strand blocks) and kept only when the lift is unique
and the target reference base is the ancestral or derived allele. The ancient
tables drop C>T and G>A markers, as upstream does.

### Refreshing the YFull tree

1. Clone [YFullTeam/YTree](https://github.com/YFullTeam/YTree) and download
   YBrowse's `snps_hg38.csv` into `scratch/`. Have indexed hg38, hg19 and T2T
   (CHM13v2.0) FASTAs, Yleaf's liftover chains (in an upstream Yleaf checkout's
   `yleaf/data`) and the Yleaf v4.1.6 data cache at hand.
2. Build the pack and review `build_info.json`'s counts (capture under `out/`).
   The tarball is byte-reproducible from the same inputs:

   ```sh
   pixi run python scripts/build_yfull_tree.py --ytree scratch/YTree \
     --version 14.06.0 --ybrowse scratch/ybrowse/snps_hg38.csv --ybrowse-date 2026-11-05 \
     --chain-hg19 scratch/Yleaf/yleaf/data/hg19/hg38ToHg19.over.chain.gz \
     --chain-t2t scratch/Yleaf/yleaf/data/t2t/hg38ToHs1.over.chain.gz \
     --ref-hg38 hs38DH.fa.gz --ref-hg19 hg19.fa.gz --ref-t2t chm13v2.0.fa.gz \
     --major-tables <reference_library>/yleaf/v4.1.6/hg_prediction_tables/major_tables \
     --output out/yleaf-tree/14.06.0
   ```

3. Benchmark the pack against the previous tree on male WGS with known
   haplogroups (for example 1000 Genomes, Poznik et al. 2016).
4. Create a draft, non-latest release `yleaf-tree-yfull-v<version>` with the
   tarball, `build_info.json`, `LICENSE-YTree.md` and `SHA256SUMS`, check the
   assets, then publish it with
   `--draft=false --latest=false`.
5. Rerun with `--write-manifest`, which updates `trees.json`, and commit.

## Engine choice

Native Yleaf is the only Y-haplogroup engine. In November 2026 (#359) it was
benchmarked on 36 1000 Genomes males (30x and downsampled to about 6x, against
Poznik et al. 2016) and on GIAB HG002/HG003, against
[yallhap](https://pypi.org/project/yallhap/) 1.0.1 and
[ybyra](https://github.com/tpinotti/ybyra) v0.6.0:

- Yleaf with YFull 14.06.0 and ybyra placed all 72 runs consistently with the
  published calls; yallhap misplaced one Q sample into R-M269 at both depths.
- Call depth was similar. yallhap or ybyra went one level deeper on 2-3
  samples, where Yleaf's 0.95 QC2 rule rejects a node whose YFull equivalents
  include SNPs that YBrowse assigns to an unsplit subclade. Yleaf was deeper on
  more samples than either.
- Yleaf was about 8x faster than yallhap and 10-15x faster than ybyra, and used
  0.5 GB against yallhap's 3.9 GB.
- yallhap is PolyForm Noncommercial and cannot ship with this GPL code. ybyra
  is a Snakemake workflow with an older bundled YFull 13 tree.

Neither is a clear improvement, so no optional `--engine` was added. The full
results are in the issue.

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
