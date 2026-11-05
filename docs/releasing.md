# Releasing

A CLI release reaches app users in two steps: the CLI release itself, then a
WGS Extract app release (gui-for-cli) that bundles it. Most of it is automated;
this is the whole sequence, with what to check at each step.

## 1. The CLI release

Merging the release PR is the whole CLI release: nobody has to push a tag or
run `gh release`, so an agent session that can only open and merge PRs can do
it.

1. **Bump the version** on a branch named `release-X.Y.Z`:

   ```bash
   python scripts/bump_version.py X.Y.Z
   ```

   It sets the version in `pyproject.toml`, `pixi.toml` and
   `src/wgsextract_cli/__init__.py`, the three files the release workflow
   checks.

2. **Write the release notes** in `docs/release-notes/vX.Y.Z.md`, in the same
   PR: what users get, under `## New` and `## Fixes` (see v0.4.2 and v0.4.3),
   ending with a compare link (`.../compare/vPREVIOUS...vX.Y.Z`). The PRs since
   the last release are `git log --oneline vPREVIOUS..origin/main`.

3. **Open a PR** titled `Release X.Y.Z` and merge it once CI is green.

   On the merge, `release.yml` sees the new version, runs lint, type checks,
   tests and the package smoke test on the merge commit, then creates the
   `vX.Y.Z` tag and publishes the release as the latest with the notes file.
   `install.sh` installs the latest published release. The native Windows
   tools aren't part of it: `pixi.toml` pins their own packages (see
   [packaging/windows/README.md](../packaging/windows/README.md)).

   Check the *Release* run on the merge commit. The tag isn't created until
   the checks pass. If the run fails, fix the cause and retry from main with
   *Run workflow* on the Release workflow (`gh workflow run release.yml`): it
   picks up where it stopped, publishing from the existing tag if one was
   already created. A release only becomes the latest if no newer version
   already is.

Without a notes file the release is left as a **draft** with generated notes.
Edit and publish it on the release's GitHub page (**Edit**, then **Publish
release**), or with
`gh release edit vX.Y.Z --notes-file notes.md --draft=false --latest`.
Pushing a `vX.Y.Z` tag by hand still works too, and also uses the notes file
if the tagged commit has one.

## 2. The app release (gui-for-cli)

1. **The bump PR.** Within the hour, gui-for-cli's *WGSExtract CLI release
   watch* runs `scripts/bump-wgsextract-cli.py vX.Y.Z` and pushes a
   `cli-vX.Y.Z` branch. GitHub Actions isn't allowed to open PRs there, so the
   job then fails once with a link to open the PR: that failure email is the
   notification. To start it by hand instead:
   `gh workflow run wgsextract-cli-release-watch.yml -R WGSExtract/gui-for-cli`.

   Open the PR, replace its CHANGELOG entry's link with a line on what users
   get, and merge once CI is green. CI runs normally, since a person opened the
   PR.

2. **Tag the app** once gui-for-cli's `main` CI passes on the merge commit, in
   an up-to-date gui-for-cli checkout (not this repository's):

   ```bash
   cd ../gui-for-cli && git switch main && git pull --ff-only
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
user's machine does, with the CLI release it bundles. The Windows app jobs only
collect evidence; **windows-judge** checks it (and the negative controls), so
the evidence the release works is the pinned jobs and windows-judge passing. See [install_testing.md](install_testing.md)
for what the lab checks, and for trying an app fix before releasing it
(`gh workflow run install_lab.yml -f app_run=<Distribution run id>`).

## Optional tool releases

Native tool builds are released separately from the CLI, as prereleases that
never become the latest release:

- **Windows tools** (`windows-tools-N`): Actions > Windows Tools > Run
  workflow. See [windows.md](windows.md).
- **macOS aligners** (`macos-aligners-N`): Actions > macOS Aligners > Run
  workflow. It publishes the Apple Silicon `wgse-bwa` and `wgse-bwa-mem2`
  tarballs built from `native/` (see [native/README.md](../native/README.md)).
  The CLI does not use them yet. Each package carries its own version, printed
  by `--version`: bump `WGSE_BWA_VERSION` in `native/bwa/CMakeLists.txt` or the
  project version in `native/CMakeLists.txt` (`wgse-bwa-mem2`) when it changes.
