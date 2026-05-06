# macOS App / DMG Packaging Handoff

## Summary

This branch adds a first-pass macOS drag-and-drop app installer for WGS Extract. The output is a DMG containing `WGS Extract.app`, an `/Applications` symlink for drag-and-drop installation, a `WGS Extract CLI.command` Terminal helper, and a short README.

The app intentionally stays Pixi-based instead of freezing Python into a PyInstaller or py2app binary. The app bundle contains a source snapshot of this repository and launches the desktop GUI with Pixi. On first launch, it copies the bundled source snapshot into a writable runtime directory under the user's Application Support folder, then runs the GUI from there.

The build is designed to be run by developers and CI, not by end users.

## Files Added Or Changed

- `.github/workflows/macos_app_release.yml`
- `.github/workflows/macos_app_pr_ci.yml`
- `packaging/macos/app_env.sh`
- `packaging/macos/launch_gui.sh`
- `packaging/macos/open_cli.command`
- `packaging/macos/README.md`
- `scripts/build_macos_app.sh`
- `pixi.toml`
- `README.md`
- `handoff.md`

## How To Build Locally

Run this from the repository root on macOS:

```bash
pixi run build-macos-app
```

Expected outputs:

```text
out/macos/WGS Extract.app
out/macos/WGS-Extract-0.1.0-macOS.dmg
out/macos/WGS-Extract-0.1.0-macOS.dmg.sha256
```

The exact DMG name tracks the version in `pyproject.toml`.

To build only the `.app` bundle without creating the DMG:

```bash
pixi run build-macos-app --no-dmg
```

## How The App Works

The app bundle has this important structure:

```text
WGS Extract.app/
  Contents/
    Info.plist
    MacOS/
      WGS Extract
    Resources/
      AppIcon.icns
      app_env.sh
      app/
        pyproject.toml
        pixi.toml
        pixi.lock
        README.md
        LICENSE
        src/
        .wgsextract-app-build
```

`Contents/MacOS/WGS Extract` is copied from `packaging/macos/launch_gui.sh`.

At launch, it:

1. Initializes a launcher log under `~/Library/Logs/WGS Extract/`.
2. Locates Pixi from common locations such as `~/.pixi/bin/pixi`, `~/.local/bin/pixi`, `/opt/homebrew/bin/pixi`, `/usr/local/bin/pixi`, or `PATH`.
3. Shows a macOS dialog if Pixi is missing.
4. Copies `Contents/Resources/app` into `~/Library/Application Support/WGS Extract/runtime/<version>/app` when the runtime is missing or stale.
5. Runs `pixi run --manifest-path <runtime>/pixi.toml wgsextract gui --desktop`.
6. Logs stdout and stderr to the launcher log file.
7. Shows a macOS dialog pointing at the log if the GUI exits non-zero.

The writable runtime copy is important because Pixi creates environment and cache state, and writing inside a mounted DMG or `/Applications` app bundle is not reliable.

## CLI Helper

The DMG includes `WGS Extract CLI.command`, copied from `packaging/macos/open_cli.command`.

When double-clicked, it opens Terminal, finds `WGS Extract.app` next to itself or in `/Applications`, prepares the same writable runtime, and starts an interactive zsh session with this function:

```bash
wgsextract() {
    "<pixi>" run --manifest-path "<runtime>/pixi.toml" wgsextract "$@"
}
```

This lets users run commands like:

```bash
wgsextract --help
wgsextract info --input /path/to/sample.bam
wgsextract gui --desktop
```

The helper is included because the user specifically wanted to be able to shell out to a terminal and interact with the WGS Extract CLI.

## GitHub Actions Workflow

`.github/workflows/macos_app_release.yml` runs on:

- `workflow_dispatch`
- published GitHub releases

The workflow:

1. Checks out the repo.
2. Installs Pixi via `prefix-dev/setup-pixi@v0.9.5`.
3. Runs `pixi run build-macos-app`.
4. Uploads the DMG and `.sha256` as workflow artifacts.
5. If triggered by a release publish event, attaches the DMG and checksum to that release with `gh release upload --clobber`.

`.github/workflows/macos_app_pr_ci.yml` is a dedicated PR/dispatch validation workflow for the app packaging. It runs on pull requests targeting `main` or this continuation branch base (`macos-app-dmg-installer`) and on `workflow_dispatch`. It validates the packaging shell scripts, builds the app/DMG on `macos-latest`, inspects the app bundle, runs the bundled CLI help command, mounts the DMG, checks the drag-and-drop contents, and uploads the DMG/checksum as artifacts.

## Validation Already Performed

The following checks passed locally on macOS:

```bash
bash -n scripts/build_macos_app.sh
bash -n packaging/macos/app_env.sh
bash -n packaging/macos/launch_gui.sh
bash -n packaging/macos/open_cli.command
pixi run build-macos-app
hdiutil verify out/macos/WGS-Extract-0.1.0-macOS.dmg
pixi run --manifest-path "out/macos/WGS Extract.app/Contents/Resources/app/pixi.toml" wgsextract --help
pixi run ruff check .
pixi run mypy src/wgsextract_cli
```

For this continuation PR, the Linux baseline also passed before adding the PR workflow:

```bash
pixi run test
pixi run lint
pixi run typecheck
```

The new `PR macOS App Packaging` workflow was created and triggered for PR #22, but GitHub completed the first run with `action_required` before scheduling jobs, which indicates the workflow needs repository/maintainer approval before its macOS jobs can execute.

The DMG was also mounted read-only and inspected. It contained:

```text
Applications -> /Applications
README.md
WGS Extract CLI.command
WGS Extract.app
```

## Lessons Learned

- Do not try to run Pixi from inside the read-only DMG mount directly for normal app use. Copy the source snapshot to a writable Application Support runtime first.
- Double-clicked `.app` processes do not inherit the user's interactive shell environment, so the launcher must search common Pixi install locations instead of relying only on `PATH`.
- The GUI already shells out to child `wgsextract` commands using the current Python executable. Running the GUI through `pixi run wgsextract gui --desktop` preserves the Pixi environment for those child processes.
- The first launch can take a while because Pixi may need to solve or create the environment. The launcher sends a macOS notification so users are not left with no feedback.
- `Info.plist` generation is easy to break with literal `\n` text in shell variables. Keep XML fragments as real multiline shell strings or generate the plist another way.
- `out/` is gitignored and is the correct location for generated DMGs, logs, mount checks, and scratch packaging output.
- For repeated local dev builds of the same version, the app runtime can become stale. The builder writes `.wgsextract-app-build` with version, source revision, dirty marker, and build timestamp; the launcher compares this marker and refreshes the runtime copy when it changes.

## Known Limitations

- The app is ad-hoc signed by default with `codesign --sign -`. This is enough to produce a coherent local app bundle, but it does not prevent Gatekeeper warnings for downloaded release artifacts.
- There is no notarization yet. For a public macOS release, add Developer ID signing and Apple notarization.
- Pixi is still a required end-user dependency. The app validates that Pixi exists and gives install guidance, but it does not install Pixi for the user.
- The first launch may be slow because Pixi may need to prepare the environment.
- The release workflow has not yet been exercised from an actual GitHub release event in this branch. It should be validated after the PR lands or from a test release.
- The app currently launches the desktop CustomTkinter GUI. The web GUI is not used because README marks it incomplete and broken.
- There is no custom DMG background image or polished Finder window layout yet. The current DMG is functionally correct but visually plain.

## Recommended Next Steps

1. Run the GitHub Actions workflow with `workflow_dispatch` on this branch and confirm the artifact downloads cleanly.
2. On a clean macOS machine with Pixi installed, download the DMG artifact, drag `WGS Extract.app` to `/Applications`, and double-click it.
3. Confirm first-launch runtime creation at `~/Library/Application Support/WGS Extract/runtime/<version>/app`.
4. Confirm launcher logs under `~/Library/Logs/WGS Extract/` are useful when failures occur.
5. Exercise `WGS Extract CLI.command` from the mounted DMG and from `/Applications` after copying the app.
6. Decide whether to add a `pixi.lock` update policy for release builds. The builder copies `pixi.lock` when present.
7. Add Developer ID signing and notarization when release credentials are available.
8. Optionally polish the DMG with a background image and positioned Finder icons.

## Signing And Notarization Notes

The current script supports overriding the signing identity with:

```bash
CODESIGN_IDENTITY="Developer ID Application: Example Name (TEAMID)" pixi run build-macos-app
```

To skip signing entirely:

```bash
WGSEXTRACT_SKIP_CODESIGN=1 pixi run build-macos-app
```

Future notarization work probably belongs in the GitHub Actions workflow after `pixi run build-macos-app`, using secrets for Apple ID issuer/key credentials or an App Store Connect API key. After notarization, staple the ticket to `WGS Extract.app` before creating the DMG, or notarize the DMG itself depending on the chosen release flow.

## Important Commands For Another Agent

Inspect the app bundle:

```bash
plutil -p "out/macos/WGS Extract.app/Contents/Info.plist"
find "out/macos/WGS Extract.app" -maxdepth 4 -type f
```

Mount and inspect the DMG:

```bash
mkdir -p out/macos/mount-check
hdiutil attach "out/macos/WGS-Extract-0.1.0-macOS.dmg" -mountpoint out/macos/mount-check -nobrowse -readonly
ls -la out/macos/mount-check
hdiutil detach out/macos/mount-check
```

Validate the bundled source snapshot can run the CLI:

```bash
pixi run --manifest-path "out/macos/WGS Extract.app/Contents/Resources/app/pixi.toml" wgsextract --help
```

Re-run full validation:

```bash
pixi run build-macos-app
pixi run ruff check .
pixi run mypy src/wgsextract_cli
```

## Where To Look If Something Breaks

- Build failures: `scripts/build_macos_app.sh`
- App launch failures: `packaging/macos/launch_gui.sh`
- Shared Pixi/runtime lookup logic: `packaging/macos/app_env.sh`
- CLI helper failures: `packaging/macos/open_cli.command`
- CI release artifact behavior: `.github/workflows/macos_app_release.yml`
- PR macOS app validation behavior: `.github/workflows/macos_app_pr_ci.yml`
- User-facing build notes: `README.md` and `packaging/macos/README.md`
- Runtime launch logs on a user's Mac: `~/Library/Logs/WGS Extract/launcher-*.log`
- Writable runtime copy on a user's Mac: `~/Library/Application Support/WGS Extract/runtime/<version>/app`
