# Releasing

A CLI release reaches app users in two steps: the CLI release itself, then a
WGS Extract app release (gui-for-cli) that bundles it. Most of it is automated;
this is the whole sequence, with what to check at each step.

## 1. The CLI release

1. **Bump the version** on a branch named `release-X.Y.Z`:

   ```bash
   python scripts/bump_version.py X.Y.Z
   ```

   It sets the version in `pyproject.toml`, `pixi.toml` and
   `src/wgsextract_cli/__init__.py`, the three files the release workflow
   checks. Open a PR titled `Release X.Y.Z` that lists the PRs since the last
   tag (`git log --oneline vPREVIOUS..origin/main`), and merge it once CI is
   green.

2. **Tag the merge commit** on `main` and push the tag:

   ```bash
   git switch main && git pull
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```

   `release.yml` checks that the tag matches all three versions, runs lint,
   type checks, tests and the package smoke test, and creates a **draft**
   release with generated notes.

3. **Write the release notes.** Replace the generated PR list with what users
   get, under `## New` and `## Fixes` (see v0.4.2 and v0.4.3), and end with a
   compare link (`.../compare/vPREVIOUS...vX.Y.Z`).

4. **Publish** the draft as the latest release:

   ```bash
   gh release edit vX.Y.Z --notes-file notes.md --draft=false --latest
   ```

   `install.sh` installs the latest published release. Publishing also starts
   **Release Windows Native Tools** (`release_windows_bwa.yml`), which attaches
   the Windows BWA, minimap2, samblaster and fastp builds that pacman installs
   download from the release. Wait for it to pass before step 2 below.

## 2. The app release (gui-for-cli)

1. **The bump PR.** Within the hour, gui-for-cli's *WGSExtract CLI release
   watch* runs `scripts/bump-wgsextract-cli.py vX.Y.Z` and pushes a
   `cli-vX.Y.Z` branch. GitHub Actions isn't allowed to open PRs there, so the
   job then fails once with a link to open the PR: that failure email is the
   notification. To start it by hand instead:
   `gh workflow run wgsextract-cli-release-watch.yml -R theontho/gui-for-cli`.

   Open the PR, replace its CHANGELOG entry's link with a line on what users
   get, and merge once CI is green. CI runs normally, since a person opened the
   PR.

2. **Tag the app** once gui-for-cli's `main` CI passes on the merge commit:

   ```bash
   git tag -a vA.B.C -m "vA.B.C ..." && git push origin vA.B.C
   ```

   The Distribution workflow builds, signs and publishes the installers. The
   app's version comes from the tag.

## 3. Proof it reached users

Within the hour of the app release, this repo's *App release watch*
(`app_release_watch.yml`) starts a full Install Lab run named
`Install Lab: app vA.B.C`: Linux, macOS and Windows, with the CLI from this
tree and with the CLI release the app pins, plus the negative controls. To
start it at once: `gh workflow run app_release_watch.yml`.

The **pinned** jobs are the ones that matter: they set up the app exactly as a
user's machine does, with the CLI release it bundles. All of them passing is
the evidence the release works. See [install_testing.md](install_testing.md)
for what the lab checks, and for trying an app fix before releasing it
(`gh workflow run install_lab.yml -f app_run=<Distribution run id>`).
