---
output: 'index.html'
title: 'WGS Extract CLI | Local-first whole genome toolkit'
description: 'WGS Extract CLI and GUI documentation, install guide, workflows, and whole genome sequencing reference.'
eyebrow: 'Local-first genomics toolkit'
heading: 'Turn whole genome data into useful files, reports, and extracts.'
lede: 'WGS Extract CLI is a modern, scriptable command-line recreation of WGS Extract with a desktop GUI for guided workflows. It wraps common bioinformatics tools so you can inspect BAM/CRAM files, build consumer microarray files, call variants, extract Y/MT reads, manage references, and automate repeatable genome work.'
footer_title: 'WGS Extract CLI'
footer_text: 'Local-first tools for practical whole genome sequencing workflows.'
footer_extra: 'GPL-3.0-or-later. Source on [GitHub](https://github.com/theontho/wgsextract-cli).'
footer_link_text:
footer_link_href:
auto_hero: false
---

::: raw
<header class="hero">
  <div class="wrap hero-grid">
    <div>
      <p class="eyebrow"><span class="pulse" aria-hidden="true"></span> Local-first genomics toolkit</p>
      <h1>Turn whole genome data into useful files, reports, and extracts.</h1>
      <p class="lede">
        WGS Extract CLI is a modern, scriptable command-line recreation of WGS Extract with a desktop GUI for guided workflows. It wraps common bioinformatics tools so you can inspect BAM/CRAM files, build consumer microarray files, call variants, extract Y/MT reads, manage references, and automate repeatable genome work.
      </p>
      <div class="actions">
        <a class="btn primary" href="install.html">Start installing</a>
        <a class="btn" href="workflows.html">Browse workflows</a>
        <a class="btn" href="wgs-guide.html">Learn WGS basics</a>
      </div>
    </div>

    <aside class="hero-card" aria-label="Quick start terminal">
      <div class="terminal">
        <div class="terminal-top" aria-hidden="true"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
        <pre><code><span class="comment"># Open Terminal, paste this line, then press Enter</span>
<span class="prompt">$</span> <span class="cmd">curl -fsSL https://raw.githubusercontent.com/theontho/wgsextract-cli/main/install.sh | sh</span>

<span class="comment"># Check your setup</span>
<span class="prompt">$</span> <span class="cmd">./wgsextract-cli/wgsextract info --detailed</span>

<span class="comment"># Launch desktop GUI:
# macOS: double-click WGS Extract GUI.command in Finder
# Linux:</span>
<span class="prompt">$</span> <span class="cmd">./wgsextract-cli/start-wgsextract-gui.sh</span></code></pre>
      </div>
      <div class="stats">
        <div class="stat"><strong>CLI</strong><span>Automation first</span></div>
        <div class="stat"><strong>GUI</strong><span>Guided desktop app</span></div>
        <div class="stat"><strong>Pixi</strong><span>Reproducible tools</span></div>
      </div>
    </aside>
  </div>
</header>
:::

::: section
::: wrap
::: section-head
## A practical reference site, not just a splash page.

Use the pages below as a map for installing the tool, choosing the right interface, understanding WGS file types, and running common genome workflows safely.
:::

::: grid three
::: card
{{ kicker: Install }}
### Install the standalone app or use Pixi directly
Use the standalone macOS/Linux installer, the native Windows installer, or the developer Pixi workflow when you want a normal source checkout.

{{ link: Read the install guide|install.html|inline-link }}
:::

::: card
{{ kicker: CLI reference }}
### Command groups and recipes
Learn the major command families for info, BAM/CRAM, extraction, VCF calling, annotation, lineage, FASTQ QC, references, and fake data.

{{ link: Open the CLI guide|cli.html|inline-link }}
:::

::: card
{{ kicker: GUI guide }}
### Use the desktop interface
Understand the GUI tabs, what each tab does, when the GUI is easier than the CLI, and how it maps back to command-line workflows.

{{ link: Explore the GUI|gui.html|inline-link }}
:::

::: card
{{ kicker: Workflows }}
### Go from a goal to commands
Follow recipes for microarray simulation, Y-DNA and mtDNA extraction, variant calling, FASTQ to BAM, storage conversion, and testing.

{{ link: Pick a workflow|workflows.html|inline-link }}
:::

::: card
{{ kicker: WGS guide }}
### Learn whole genome concepts
Get a friendly explanation of whole genome sequencing, coverage, references, variants, file types, read technologies, and interpretation limits.

{{ link: Read the WGS guide|wgs-guide.html|inline-link }}
:::

::: card
{{ kicker: Reference }}
### Glossary and safety notes
Look up common terms, file extensions, external tools, privacy considerations, and troubleshooting tips for long-running genome jobs.

{{ link: Open the reference|reference.html|inline-link }}
:::
:::
:::
:::

::: section
::: showcase
::: screenshot
{{ screenshot: gui-screenshot.jpg|Screenshot of the WGS Extract desktop GUI }}
:::

::: block
{{ kicker-p: CLI plus GUI }}
## Choose the interface that fits the job.

{{ lede: Use the CLI for repeatable automation, batch runs, remote machines, and precise region testing. Use the desktop GUI when you want a tabbed interface for selecting files, browsing references, launching common jobs, and watching logs. }}

::: button-row
{{ button: CLI reference|cli.html|primary }}
{{ button: GUI reference|gui.html }}
:::
:::
:::
:::
