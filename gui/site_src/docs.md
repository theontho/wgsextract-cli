---
output: 'docs.html'
title: 'GUI for CLI documentation map'
description: 'Documentation map for GUI for CLI: the app, bundles, distribution, and static site generation.'
eyebrow: 'Documentation'
heading: 'Where to go next.'
lede: 'Start with v0.6.6 downloads or one-command developer setup. Workflow details, distribution, and schemas live close to the code.'
actions: 'Open README|https://github.com/WGSExtract/wgsextract-cli/blob/main/gui/README.md|primary; Architecture|architecture.html'
footer_title: 'Documentation map'
footer_text: 'Product docs stay short; detailed engineering notes stay close to the code.'
---

::: section
::: wrap
::: grid two
::: card
{{ kicker: Product }}
### README
The top-level README describes the app, setup, common commands, bundles, config, and distribution packaging.

{{ link: Open README|https://github.com/WGSExtract/wgsextract-cli/blob/main/gui/README.md|inline-link }}
:::

::: card
{{ kicker: Development }}
### One-command setup
Clone the repo and run `make setup` on Linux/macOS, or `powershell -ExecutionPolicy Bypass -File .\make.ps1 setup` in Windows PowerShell 5.1. Without make on Linux/macOS, use `sh scripts/dev_setup.sh`.

Setup prepares missing user-level uv, Node, and Rust tools, locked dependencies, hooks, and a real Web UI smoke check. It never elevates or installs system packages; the guide lists manual desktop prerequisites. No sibling WGSExtract CLI checkout or genome download is needed for UI development: start with `examples/GitHelper`.

{{ link: Open developer setup|https://github.com/WGSExtract/wgsextract-cli/blob/main/gui/docs/developer_setup.md|inline-link }}
:::

::: card
{{ kicker: Releases }}
### Current app and CLI: v0.6.6
Download installers and read the complete GUI and bundled CLI release notes. The changelog tracks the move to one Tauri app, native Windows setup, macOS notarization, and bundle improvements.

{{ link: Open releases|https://github.com/WGSExtract/gui-for-cli/releases|inline-link }}
{{ link: Open changelog|https://github.com/WGSExtract/wgsextract-cli/blob/main/gui/docs/CHANGELOG.md|inline-link }}
:::

::: card
{{ kicker: WGSExtract }}
### Current workflows
Nanopore handling in existing actions, Clair3 and methylation controls, plus opt-in MultiQC, expanded microarray/AADR exports, targeted Y analysis, and resource requirements. See what is in the app and what is still CLI only.

{{ link: WGSExtract workflows|wgsextract.html|inline-link }}
{{ link: Post-v0.6.3 feature details|https://github.com/WGSExtract/wgsextract-cli/blob/main/gui/docs/wgsextract-post-v063.md|inline-link }}
:::

::: card
{{ kicker: Schemas }}
### Bundle schemas
`manifest.json` and page files are described by JSON Schemas in `docs/schema/`.

{{ link: Open schemas|https://github.com/WGSExtract/wgsextract-cli/tree/main/gui/docs/schema|inline-link }}
:::

::: card
{{ kicker: Archive }}
### Earlier experiments
The SwiftUI app, Swift CLI, prototype renderers, and benchmark reports live on the `old-experimental` branch.

{{ link: Open the archive branch|https://github.com/WGSExtract/gui-for-cli/tree/old-experimental|inline-link }}
:::

::: card
{{ kicker: Distribution }}
### Packaging
Signing, notarization, Tauri packages for each OS, release artifacts, self-updates, and embedded bundle defaults.

{{ link: Open distribution doc|https://github.com/WGSExtract/wgsextract-cli/blob/main/gui/docs/distribution.md|inline-link }}
:::
:::
:::
:::

::: section
::: wrap
## Generate this site

Edit Markdown in `site_src/`, then run:

```bash
python3 scripts/validate_site_sources.py
python3 scripts/build_site.py
open site/index.html
```

The dependency-free generator writes HTML to `site/` for GitHub Pages. Commit regenerated HTML alongside source changes; `make site` is also available.

{{ link: Site generation guide|https://github.com/WGSExtract/wgsextract-cli/blob/main/gui/docs/site_generation.md|inline-link }}
:::
:::
