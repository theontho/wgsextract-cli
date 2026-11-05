---
output: 'architecture.html'
title: 'GUI for CLI architecture'
description: 'GUI for CLI is one TypeScript app, packaged with Tauri for Windows, macOS, and Linux.'
eyebrow: 'Architecture'
heading: 'One TypeScript app, three desktops.'
lede: 'GUI for CLI renders a CLI-tool bundle with a TypeScript Web UI and a local Node backend, and Tauri packages both as a native desktop app for Windows, macOS, and Linux.'
actions: 'WGSExtract story|wgsextract.html|primary; Documentation|docs.html'
footer_title: 'One codebase'
footer_text: 'Bundle behavior lives in the shared TypeScript runtime; Tauri only hosts the window, menus, pickers, and updates.'
---

::: section
::: wrap
::: grid three
::: card
{{ kicker: Runtime }}
### Shared TypeScript
Bundle loading, localization, rendering, setup state, configuration, and command execution live in one TypeScript codebase.
:::

::: card
{{ kicker: Backend }}
### Local Node server
A Node server runs bundle actions and platform-specific setup scripts, streams terminal output, and serves the Web UI on a loopback port behind a per-launch token. Packaged apps include Node; users do not need a separate Node installation.
:::

::: card
{{ kicker: Desktop }}
### Tauri shell
Tauri wraps the Web UI in a native window, bundles the Node runtime, and adds native menus, file pickers, installers, and signed self-updates.
:::
:::
:::
:::

::: section
::: wrap
## From bundle to desktop

- `app/shared` holds shared rendering, localization, and types; `app/web/src/client` is the browser UI and `app/web/src/server` owns loading, setup, actions, and config.
- Every bundle load validates its manifest and pages. **Load Bundle...** opens a `manifest.json` or a `.zip`, `.tar`, `.tar.gz`/`.tgz`, or gzipped manifest archive on all three platforms.
- Bundle scripts resolve by host platform, with distro-specific Linux folders and a POSIX fallback. WGSExtract CLI v0.6.6 runs natively on Windows without WSL or MSYS2.
- Configuration and workspaces stay in platform-standard app data directories. WGSExtract settings preserve TOML boolean and numeric types; invalid saved types warn and use defaults without rewriting the file.
- `app/web/packagers/tauri` supplies the Rust desktop shell. Installers take their app name and version from the embedded bundle; the current WGSExtract app and CLI are v0.6.6.

Tagged releases build on Windows, macOS, and Linux with a signed Tauri updater feed. macOS has separate signed, notarized Apple Silicon and Intel builds, not a universal DMG. Windows and Linux packages are x64; native ARM64 packaging for those platforms is still a follow-up.

[Distribution details](https://github.com/WGSExtract/gui-for-cli/blob/main/docs/distribution.md) cover packaging, signing, updates, and uninstall hooks. Earlier SwiftUI, Sparkle, and Electron implementations are not part of the current app.
:::
:::

::: section
::: wrap
## Running it

```bash
make setup    # user-level tools, locked dependencies, hooks, smoke check
make run      # Web UI in a browser
make tauri    # desktop app in dev mode
make package  # installers for this host
```

On Windows, use `powershell -ExecutionPolicy Bypass -File .\make.ps1 <target>` with the same target names. Setup never elevates or installs system packages; see [developer setup](https://github.com/WGSExtract/gui-for-cli/blob/main/docs/developer_setup.md) for OS prerequisites.
:::
:::

::: section
::: wrap
## Earlier experiments

Before settling on one app, the project prototyped native SwiftUI and AppKit apps, a Swift CLI, Electron, and renderers in Rust, Go, C++, .NET, Dart, Kotlin, Python, and Mojo, with benchmarks across all of them. That work is preserved on the [`old-experimental`](https://github.com/WGSExtract/gui-for-cli/tree/old-experimental) branch.
:::
:::
