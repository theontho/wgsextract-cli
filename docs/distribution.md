# Distribution packaging

GUI for CLI ships one Tauri desktop app, built natively on each OS:

- **macOS** → `.app`, `.dmg`, and a signed updater archive (`<App>_<arch>.app.tar.gz`)
- **Linux** → `.deb`, `.rpm`, Arch Linux `.pkg.tar.zst`, and `.AppImage`
- **Windows** → NSIS installer `.exe`

## Local commands

Build distributables for the current host into `out/release/tauri/`:

```bash
make package           # macOS / Linux
.\make.ps1 package     # Windows
```

`make package-windows-cross` builds the Windows x64 NSIS installer on macOS or
Linux (into `out/release/tauri-windows`), cross-compiled with
[cargo-xwin](https://github.com/rust-cross/cargo-xwin) and NSIS as Tauri
documents for non-Windows hosts. It needs `cargo-xwin`, `makensis`, LLVM's
`clang-cl`/`llvm-rc`/`lld-link` on `PATH`, and the `x86_64-pc-windows-msvc`
Rust target, and checks for them first. The installer is unsigned; the WGS
Extract install lab uses it to test the Windows app from a Mac.

On Linux, Tauri does not provide a native Arch bundle target, so the Arch
package installs the generated AppImage under `/opt/<package>` with a
pacman-managed launcher and desktop entry.

### Embedded bundle and app identity

Packaged releases default to the bundled WGSExtract app while generic distribution mode is still pending. To use a different embedded bundle, set these in `.devconfig.toml`:

```toml
[packaging]
embedded_bundle_path = "examples/WGSExtract"
app_name = "WGSExtract"
```

The branded name is used for the app bundle name, installer/DMG name, and native window title. If you omit `app_name`, packaging falls back to the bundle directory name. `TAURI_PRODUCT_SUFFIX` appends a suffix (for example `Ubuntu WebUI`) for side-by-side test builds.

Packaging reads `version` from the bundle's `manifest.json` and uses it as the app version. For example, the bundled WGSExtract manifest currently sets `"version": "0.6.3"`, so installers are named with `0.6.3` instead of GUI for CLI's own version. Bundle-authored JSON files support `//` and `/* ... */` comments. Use SemVer-compatible values because Tauri/NSIS and macOS marketing versions reject arbitrary tags. Set `app_version` in `.devconfig.toml` or `PACKAGE_APP_VERSION` / `EMBEDDED_APP_VERSION` to override the manifest version.

Embedded bundle scripts can be split by platform under `scripts/windows`, `scripts/macos`, `scripts/linux`, `scripts/linux/<distro>`, and `scripts/posix`. Runtime resolution chooses the most specific folder for the host and falls back to `posix` for POSIX platforms. Every platform folder present in a bundle must include the full referenced script set, otherwise bundle validation fails.

Bundles can define `uninstall.steps` with the same step shape as `setup.steps`. The app exposes those as uninstall hooks for cleaning runtime state that lives outside normal app binaries or cannot be safely handled by deleting the bundle workspace alone.

## Signing and notarization

### macOS

Tauri signs the app when a signing identity is available, from (in order)
`TAURI_MACOS_SIGNING_IDENTITY`, `APPLE_SIGNING_IDENTITY`, or `.devconfig.toml`:

```toml
[apple.signing]
signing_identity = "Developer ID Application: Your Name (YOURTEAMID)"
```

`uv run python scripts/dev.py signing autosetup` detects your Xcode team and
keychain identities and writes that section. It also reports expired identities;
remove them from the keychain search list with
`uv run python scripts/dev.py signing delete-expired-identities` (`--dry-run` to
preview).

Tauri notarizes when it finds notarization credentials in the environment:
either an App Store Connect API key (`APPLE_API_KEY`, `APPLE_API_ISSUER`,
`APPLE_API_KEY_PATH`) or an Apple ID (`APPLE_ID`, `APPLE_PASSWORD`,
`APPLE_TEAM_ID`). See [Tauri's macOS signing guide](https://v2.tauri.app/distribute/sign/macos/).

### Linux and Windows

No signing is required to build the Linux packages or the Windows NSIS installer.

### Windows installer lifecycle test

`.github/workflows/tauri-installer.yml` builds the NSIS installer, silently
installs it, launches the packaged app, runs bundle setup, runs bundle uninstall
hooks, runs the native uninstaller, and checks cleanup. Run the same check
locally on Windows with
`pwsh -File scripts\validate-windows-tauri-lifecycle.ps1 -AutomatedAdmin` after
`.\make.ps1 package`.

## CI / release automation

`.github/workflows/distribution.yml` builds the Tauri app on macOS, Linux, and
Windows runners for `workflow_dispatch` and `v*` tags.

The macOS job signs and notarizes when these secrets are present (tagged
releases fail without them):

- `APPLE_CERTIFICATE_P12`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`
- notarization, either:
  - an App Store Connect API key (preferred; no Apple ID password or 2FA):
    `APPLE_API_KEY_P8` (the `.p8` file's contents), `APPLE_API_KEY_ID`,
    `APPLE_API_ISSUER_ID`; or
  - `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD` and `APPLE_TEAM_ID`.

The workflow maps those onto the variables Tauri reads. Notarization fails
(HTTP 403) until the account holder has accepted Apple's current Program License
Agreement.

To set the certificate secrets from a Mac that has the Developer ID identity,
run `tools/packaging/posix/upload_apple_signing_secrets.sh`. It exports only
that identity directly with Apple's Security framework (macOS asks to allow
the export), encrypts it with a random password, and uploads
`APPLE_CERTIFICATE_P12`, `APPLE_CERTIFICATE_PASSWORD` and
`APPLE_SIGNING_IDENTITY` without printing them. No unrelated private keys are
exported. Xcode Command Line Tools are required for the Swift export helper.
Use `--keychain PATH` to select a custom keychain; otherwise the user's keychain
search list is used. Selection uses the valid certificate's SHA1 fingerprint,
so an expired certificate with the same name does not interfere with renewal.
Rerun it when the certificate is renewed.

GitHub updates secrets individually. If an upload fails, the matching
certificate and password remain in a private `out/apple-signing.*` directory.
Retry with `--resume <directory>` and the same `--repo OWNER/NAME`; this uploads
the same pair rather than generating a new password. Do not run Distribution
until the retry succeeds. Successful uploads remove the local recovery files;
otherwise remove those specific files once recovery is complete.

The macOS release builds natively on an Apple Silicon runner and an Intel runner, producing separate arm64 and x64 apps, DMGs, and updater archives.

### Bundling a new WGSExtract CLI release

`scripts/bump-wgsextract-cli.py vX.Y.Z` points the WGSExtract bundle at another
wgsextract-cli release: the tag setup installs, the bundle version that names the
installers, the tests that pin both, and a CHANGELOG entry. It changes nothing if
any file can't be updated.
`.github/workflows/wgsextract-cli-release-watch.yml` runs it hourly when a newer
release is published and opens the PR. PRs opened with `GITHUB_TOKEN` don't
trigger workflows, so it dispatches CI, Secret Scan and Tauri installer on the
branch, and later runs start any of them that haven't run. Opening the PR
needs **Allow GitHub Actions to create and approve pull requests** (Settings >
Actions > General); without it the job pushes the branch and fails once with a
link to open the PR by hand.

When the app release is published, Distribution appends the bundled CLI
release's notes (under `## WGSExtract CLI vX.Y.Z`) to the app release notes, so
users see what changed in the CLI without leaving the app release page. Bare
`#123` references in those notes are rewritten as `theontho/wgsextract-cli#123`
so they keep pointing at the CLI repository.

## Self-updates

The app uses Tauri's v2 updater plugin with GitHub Releases as the release
source of truth. Release builds enable updater metadata when
`TAURI_UPDATER_PUBKEY` is present and create signed updater artifacts when
`TAURI_SIGNING_PRIVATE_KEY` is present.

Generate a Tauri updater keypair once and store the private key in GitHub Actions secrets:

```bash
npm --prefix app run tauri -- signer generate -w ~/.tauri/gui-for-cli.key
```

Configure release automation with:

- `TAURI_UPDATER_PUBKEY` repository variable or secret containing the generated public key.
- `TAURI_SIGNING_PRIVATE_KEY` secret containing the private key content or path.
- optional `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` secret.
- optional `TAURI_UPDATER_ENDPOINTS` / `.devconfig.toml` `[tauri.updater] endpoint = "..."`; the default endpoint is `https://github.com/theontho/gui-for-cli/releases/latest/download/latest.json`.

When updater signing is configured, Tauri emits `.sig` files next to the macOS
`.app.tar.gz`, the Windows NSIS installer, and the Linux AppImage. Packaging
renames the macOS archive to `<App>_<arch>.app.tar.gz` so the feed can tell
architectures apart. Release publishing replaces spaces with dots in asset names.

The app includes a **Check for Updates...** menu item in the **File** menu. It
checks the configured endpoint, prompts before installing, verifies the Tauri
signature, installs the update, and restarts the app.

### GitHub Release feed generation

On `v*` tags, `.github/workflows/distribution.yml` downloads the packaged artifacts, publishes them to the GitHub Release, and runs:

```bash
python3 tools/packaging/generate_update_feeds.py \
  --repo "$GITHUB_REPOSITORY" \
  --tag "$GITHUB_REF_NAME" \
  --version "${GITHUB_REF_NAME#v}" \
  --artifacts-root release-artifacts \
  --output-dir release-artifacts/update-feeds
```

It writes `latest.json` with `windows-x86_64`, `linux-x86_64`, and
`darwin-aarch64`/`darwin-x86_64` entries for every signed artifact it finds.
Tag builds set `PACKAGE_APP_VERSION` from the tag name so the packaged apps and
the feed agree on the update version.

For end-to-end update testing before a production release, push a prerelease
tag such as `v0.1.1-updater-test.1` and build the app with updater endpoints
pointed at that tag's release assets instead of the `latest` URLs. On Windows,
`scripts/validate-windows-tauri-update.ps1` installs an older release and
drives the update.
