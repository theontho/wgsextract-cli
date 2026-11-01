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
A Node server runs bundle actions and setup scripts, streams terminal output, and serves the Web UI on a loopback port behind a per-launch token.
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
## Running it

```bash
make run      # Web UI in a browser
make tauri    # desktop app in dev mode
make package  # installers for this host
```

On Windows, use `powershell -ExecutionPolicy Bypass -File .\make.ps1 <target>` with the same target names.
:::
:::

::: section
::: wrap
## Earlier experiments

Before settling on one app, the project prototyped native SwiftUI and AppKit apps, a Swift CLI, Electron, and renderers in Rust, Go, C++, .NET, Dart, Kotlin, Python, and Mojo, with benchmarks across all of them. That work is preserved on the [`old-experimental`](https://github.com/theontho/gui-for-cli/tree/old-experimental) branch.
:::
:::
