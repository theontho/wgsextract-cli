---
output: 'gui.html'
title: 'GUI Downloads | WGS Extract CLI'
description: 'Download the separate gui-for-cli graphical interface for WGS Extract, or install the CLI-only wgsextract command-line app.'
eyebrow: 'Graphical interface'
heading: 'Use gui-for-cli when you want WGS Extract with windows, buttons, and forms.'
lede: 'The command-line app and graphical app are separate projects. Install `wgsextract-cli` when you want terminal commands and automation; install gui-for-cli from GitHub Releases when you want a graphical interface for WGS Extract workflows.'
toc: 'GUI releases|releases; Choose a download|downloads; CLI option|cli-option; How they fit|fit'
footer_title: 'WGS Extract GUI'
footer_text: 'Graphical releases are maintained in the gui-for-cli repository.'
footer_link_text: 'Open all GUI releases'
footer_link_href: 'https://github.com/theontho/gui-for-cli/releases'
---

::: section id=releases
::: split
::: block
## GUI releases live in gui-for-cli
Download the WGS Extract graphical interface from the [gui-for-cli Releases page](https://github.com/theontho/gui-for-cli/releases){.inline-link}. The latest release at the time this page was updated is [v0.1.10](https://github.com/theontho/gui-for-cli/releases/tag/v0.1.10){.inline-link}, published 2026-05-24.

Use the release page when you want the newest platform packages, signatures, release notes, or older versions. The links below point directly at the current release assets for convenience.
:::

::: actions
{{ button: Open latest GUI release|https://github.com/theontho/gui-for-cli/releases/latest|primary }}
{{ button: Browse all GUI releases|https://github.com/theontho/gui-for-cli/releases }}
{{ button: Install CLI instead|install.html }}
:::
:::
:::

::: section id=downloads
::: wrap
::: section-head
## Choose a GUI download
Pick the package that matches your operating system. If you are unsure, open the latest release page and choose the asset GitHub recommends for your platform.
:::

::: grid three
::: card
### Windows
Use the x64 setup executable for a normal graphical install.

{{ link: WGSExtract_0.1.10_x64-setup.exe|https://github.com/theontho/gui-for-cli/releases/download/v0.1.10/WGSExtract_0.1.10_x64-setup.exe|inline-link }}

The release also includes a quick uninstall PowerShell helper and signature assets.
:::

::: card
### macOS
Use a DMG package, then drag the app into Applications when prompted.

{{ link: WGSExtract-0.1.10.dmg|https://github.com/theontho/gui-for-cli/releases/download/v0.1.10/WGSExtract-0.1.10.dmg|inline-link }}

Apple Silicon users can also use the aarch64 web-app DMG when that package is the better fit.

{{ link: WGSExtract.Web_0.1.10_aarch64.dmg|https://github.com/theontho/gui-for-cli/releases/download/v0.1.10/WGSExtract.Web_0.1.10_aarch64.dmg|inline-link }}
:::

::: card
### Linux
Use the package format native to your distribution, or use the AppImage for a portable desktop app.

{{ link: AppImage|https://github.com/theontho/gui-for-cli/releases/download/v0.1.10/WGSExtract_0.1.10_amd64.AppImage|inline-link }}
{{ link: DEB|https://github.com/theontho/gui-for-cli/releases/download/v0.1.10/WGSExtract_0.1.10_amd64.deb|inline-link }}
{{ link: RPM|https://github.com/theontho/gui-for-cli/releases/download/v0.1.10/WGSExtract-0.1.10-1.x86_64.rpm|inline-link }}
{{ link: Arch package|https://github.com/theontho/gui-for-cli/releases/download/v0.1.10/wgsextract-0.1.10-1-x86_64.pkg.tar.zst|inline-link }}
:::
:::

::: callout
{{ text: **Checksums and signatures:** gui-for-cli release assets include signature, appcast, and metadata files where the GUI build publishes them. Use the GitHub release page when you need to verify a package before installing. }}
:::
:::
:::

::: section id=cli-option
::: split
::: block
## Install the CLI option
Install `wgsextract-cli` when you want terminal commands, scripts, automation, remote-machine workflows, or an explicit `wgsextract` command that you can run outside the GUI.

The standalone macOS/Linux installer creates a self-contained `wgsextract-cli/` folder with the `wgsextract` launcher beside it. Windows users should use the native `install_windows.bat` path from the install guide.
:::

::: code-panel title=install-cli.sh subtitle="macOS/Linux CLI"
```
# Open Terminal, paste this line, then press Enter.
curl -fsSL https://raw.githubusercontent.com/theontho/wgsextract-cli/main/install.sh | sh

# Verify the CLI install.
./wgsextract-cli/wgsextract info --detailed
./wgsextract-cli/wgsextract deps check
```
:::
:::

::: wrap
::: callout
{{ text: **Windows CLI:** Clone or download `wgsextract-cli`, then run `.\install_windows.bat` from PowerShell. The Windows installer bootstraps Pixi and MSYS2 when needed and configures the native UCRT64 pacman runtime. }}
:::
:::
:::

::: section id=fit
::: wrap
::: section-head
## How the GUI and CLI fit together
The GUI and CLI are intentionally separate so each can ship on the cadence that fits its users.
:::

::: grid two
::: card
### Use the GUI for interactive work
Install gui-for-cli when you want a desktop-style app for selecting inputs, setting options, and running WGS Extract workflows without writing every command by hand.
:::

::: card
### Use the CLI for repeatable work
Install `wgsextract-cli` when exact commands matter: automation, batch runs, bug reports, AI-agent workflows, remote servers, and long-running whole-genome jobs.
:::
:::
:::
:::
