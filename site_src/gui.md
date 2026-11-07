---
output: 'gui.html'
title: 'GUI Downloads | WGS Extract CLI'
description: 'Download the WGS Extract desktop app, a graphical interface for the wgsextract command-line app, or install the CLI on its own.'
eyebrow: 'Graphical interface'
heading: 'Use the WGS Extract app when you want windows, buttons, and forms.'
lede: 'The WGS Extract desktop app runs the same `wgsextract` commands from a graphical interface. Both are built from this repository; install the CLI on its own when you want terminal commands and automation.'
toc: 'Get the app|releases; Choose a download|downloads; CLI option|cli-option; How they fit|fit'
footer_title: 'WGS Extract app'
footer_text: 'The app is developed in the gui/ folder of the wgsextract-cli repository.'
footer_link_text: 'Open the app site'
footer_link_href: 'app/'
---

::: section id=releases
::: split
::: block
## Get the WGS Extract app
The [app's site](app/){.inline-link} has direct downloads for every platform, what the app does, and how it is built. From the first release after v0.6.6, each [wgsextract-cli release](https://github.com/WGSExtract/wgsextract-cli/releases){.inline-link} carries the app's installers next to the CLI it bundles; earlier app releases stay on the [former gui-for-cli repository](https://github.com/WGSExtract/gui-for-cli/releases){.inline-link}.
:::

::: actions
{{ button: Open the app site|app/|primary }}
{{ button: Install CLI instead|install.html }}
:::
:::
:::

::: section id=downloads
::: wrap
::: section-head
## Choose a download
Pick the package that matches your operating system on the [app site's download section](app/){.inline-link}.
:::

::: grid three
::: card
### Windows
Use the x64 setup executable (`_x64-setup.exe`) for a normal graphical install. A quick uninstall PowerShell helper and signature assets come with each release.
:::

::: card
### macOS
Use a DMG package, then drag the app into Applications when prompted. Choose the `aarch64.dmg` for Apple Silicon or `x64.dmg` for Intel.
:::

::: card
### Linux
Use the package format native to your distribution (`.deb`, `.rpm` or Arch package), or the AppImage for a portable desktop app.
:::
:::

::: callout
{{ text: **Checksums and signatures:** each release carries the app's updater signatures and feed (`latest.json`) next to its installers. Use the [releases page](https://github.com/WGSExtract/wgsextract-cli/releases){.inline-link} when you need to verify a package before installing. }}
:::
:::
:::

::: section id=cli-option
::: split
::: block
## Install the CLI option
Install `wgsextract-cli` when you want terminal commands, scripts, automation, remote-machine workflows, or an explicit `wgsextract` command that you can run outside the GUI. macOS and Linux use a one-line terminal installer; Windows uses `install_windows.bat`.
:::

::: actions
{{ button: Open the CLI install guide|install.html|primary }}
:::
:::
:::

::: section id=fit
::: wrap
::: section-head
## How the GUI and CLI fit together
The app is a graphical front end for the CLI: it sets up the CLI release it bundles and runs its commands. Both ship together in each release.
:::

::: grid two
::: card
### Use the GUI for interactive work
Install the WGS Extract app when you want a desktop-style app for selecting inputs, setting options, and running WGS Extract workflows without writing every command by hand.
:::

::: card
### Use the CLI for repeatable work
Install `wgsextract-cli` when exact commands matter: automation, batch runs, bug reports, AI-agent workflows, remote servers, and long-running whole-genome jobs.
:::
:::
:::
:::
