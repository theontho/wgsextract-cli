# End-to-end install testing (the install lab)

`scripts/installlab/` tests what a new user actually does: install with the README
command on a machine that has never seen WGS Extract, run real commands on a real
genome, update, uninstall, and leave the machine as it was. It is built to be run
over and over on the same computer, and to make false passes hard.

```bash
pixi run -e dev python scripts/installlab/lab.py run linux       # fresh Ubuntu container
python3 scripts/installlab/lab.py prepare macos                  # once: clean macOS base image
python3 scripts/installlab/lab.py run macos                      # fresh macOS VM
python3 scripts/installlab/lab.py selftest linux                 # negative controls must fail
python3 scripts/installlab/lab.py judge out/installlab/runs/<run>/results
```

Results land in `out/installlab/runs/<target>-<mode>-<time>/`: the guest's evidence,
state snapshots, outputs, and `verdict.json`.

## Why installs used to "pass" while being broken

| Failure mode | Defence |
| --- | --- |
| Leftover state from earlier runs or the developer's own setup (`~/.pixi`, Homebrew tools on `PATH`, caches, `config.toml`, reference data) masks a missing dependency | Every run uses a **new machine cloned from a clean image** and destroys it afterwards: a Tart VM from Cirrus Labs' *vanilla* macOS image (no Homebrew, no tools), or a fresh `ubuntu:24.04` container. Nothing survives between runs, so cleanup on the host is never needed. |
| Tests fake the expensive parts (fake `pixi`, stub `wgsextract`, `--dry-run`, mocked downloads) | The guest runs the **README command** (`curl -fsSL …/install.sh \| sh`) with the real Pixi bootstrap and real package downloads. Only the source archive and dataset come from the host, over HTTP, so the tree under test is installed exactly like a release. |
| "It exited 0" counted as success; a command that does nothing passes | The guest never judges. It records exit codes, logs and outputs; the **host oracle** checks the outputs against values that come from biology, not from a previous run: HG00096's published mtDNA haplogroup (H16a1), recall and precision against its 1000 Genomes genotypes, read and mapping counts measured with the host's own samtools/bcftools. |
| Steps silently skipped | The oracle **fails closed**: every required step must leave evidence, and a missing step fails the run. A per-run nonce ties the evidence to this run. |
| Uninstall leaves files, rc edits, caches or config behind | The guest snapshots the machine (home, `/Applications`, `/usr/local`, `/opt`, launch agents, shell rc contents, `PATH`) **before install, after install and after uninstall**. The host diffs them: after uninstall the machine must match the baseline. The after-install snapshot must show the install, which proves the snapshots can see it. |
| OS background activity makes state diffs flaky, tempting broad ignore lists | `--mode control` runs the same timeline **without installing** and prints what the OS changed by itself. Only those measured paths go in `noise/<target>.txt`, so ignore rules are evidence-based and reviewable. |
| The test harness itself is broken and passes everything | **Negative controls**: `--sabotage` deliberately breaks a run (installer does nothing, launcher returns 0 without working, samtools deleted, a file left behind, uninstall skipped). `selftest` runs them all; each must make the verdict FAIL on the expected checks. A harness that can't fail is caught. |
| Testing the developer build instead of what users get | Runs install the archive of the tree under test. The same scenario can point at a published release by leaving `LAB_ARCHIVE_URL` unset, so the installer resolves `releases/latest` like a user. |

## What one run covers

1. Baseline snapshot of a machine with only the documented prerequisites (macOS:
   Xcode Command Line Tools, baked into the base image by `prepare`; Ubuntu:
   `curl`, `ca-certificates`, `git`).
2. `curl -fsSL <installer> | sh` from the user's home directory, as a normal user.
3. The workload (`guest/workload.sh`) on the HG00096 mini genome: `--help`,
   `deps check`, `ref bootstrap` (a real GitHub release download with digest
   verification), `info` on BAM and CRAM, `extract mt-bam` / `mito-vcf`,
   `lineage mt-haplogroup` / `y-haplogroup`, `vcf snp`, `microarray`, `align`
   (bwa, from FASTQs), `vcf gatk` (first use of the optional `gatk` Pixi
   environment), and a command that must fail.
4. Update by rerunning the installer; a marker in `app/reference/` must survive,
   and the CLI must still work.
5. `uninstall.sh --yes --remove-config --remove-pixi`, then the final snapshot.

## Targets

- **macos** – Tart VM (Apple Silicon hosts). `prepare macos` clones
  `ghcr.io/cirruslabs/macos-tahoe-vanilla` and installs the Command Line Tools
  headlessly into `wgse-lab-macos-base`; each run clones that (an instant APFS
  clone), boots it headless, shares only the results and guest-script folders,
  and deletes it.
- **linux** – `ubuntu:24.04` `linux/amd64` container (Docker or OrbStack), with a
  non-root `tester` user.
- **host** – the current machine, for disposable CI runners only (refuses to run
  without `CI` set).

Windows is not covered yet; GitHub's Windows runners come with MSYS2 and other
tools preinstalled, which would mask exactly the problems this lab exists to
find.
