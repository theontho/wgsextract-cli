---
output: 'index.html'
title: 'WGSExtract v6 Alpha'
description: 'Download WGSExtract v6 Alpha v0.6.6 for Windows, macOS, and Linux, bundling WGSExtract CLI v0.6.6.'
eyebrow: 'Simple desktop downloads'
heading: 'WGSExtract v6 Alpha'
lede: 'A preview desktop app for working with whole-genome sequencing files. Pick your platform and install the latest alpha build.'
footer_title: 'WGSExtract v6 Alpha'
footer_text: 'Simple alpha downloads for the WGSExtract desktop app.'
auto_hero: false
---

::: raw
<header class="hero download-hero">
  <div class="wrap">
    <p class="eyebrow"><span class="pulse" aria-hidden="true"></span> Simple desktop downloads</p>
    <h1>WGSExtract v6 Alpha</h1>
    <p class="lede">A preview desktop app for working with whole-genome sequencing files. Pick your platform and install the latest alpha build.</p>
    <figure class="hero-screenshot">
      <img src="assets/gui-screenshot.webp" alt="Earlier WGSExtract alpha desktop app showing the FASTQ workflow in GUI for CLI." width="2648" height="2122" fetchpriority="high" />
      <figcaption class="release-note">Earlier alpha interface shown; v0.6.6 includes the workflows described below.</figcaption>
    </figure>
    <div class="download-grid" aria-label="Primary downloads">
      <article class="card download-card">
        <div class="platform-heading">
          <i class="bi bi-windows" aria-hidden="true"></i>
          <span>Windows</span>
        </div>
        <a class="primary-download" href="https://github.com/WGSExtract/gui-for-cli/releases/download/v0.6.6/WGSExtract_0.6.6_x64-setup.exe">
          <strong>Download installer</strong>
          <span>64-bit .exe setup</span>
        </a>
        <a class="download-note" href="#windows-smartscreen">
          <i class="bi bi-shield-exclamation" aria-hidden="true"></i>
          <span>Windows SmartScreen may appear. See the two-click install note.</span>
        </a>
      </article>
      <article class="card download-card">
        <div class="platform-heading">
          <i class="bi bi-apple" aria-hidden="true"></i>
          <span>macOS</span>
        </div>
        <a class="primary-download" href="https://github.com/WGSExtract/gui-for-cli/releases/download/v0.6.6/WGSExtract_0.6.6_aarch64.dmg">
          <strong>Download for Apple Silicon</strong>
          <span>Signed and notarized .dmg</span>
        </a>
        <a class="download-note" href="https://github.com/WGSExtract/gui-for-cli/releases/download/v0.6.6/WGSExtract_0.6.6_x64.dmg">Intel Mac .dmg</a>
      </article>
      <article class="card download-card linux-card">
        <div class="platform-heading">
          <i class="bi bi-terminal" aria-hidden="true"></i>
          <span>Linux</span>
        </div>
        <a class="primary-download" href="https://github.com/WGSExtract/gui-for-cli/releases/download/v0.6.6/WGSExtract_0.6.6_amd64.AppImage">
          <strong>Download AppImage</strong>
          <span>64-bit portable app</span>
        </a>
        <div class="linux-variants" aria-label="Linux package downloads">
          <a href="https://github.com/WGSExtract/gui-for-cli/releases/download/v0.6.6/WGSExtract_0.6.6_amd64.deb"><i class="bi bi-ubuntu" aria-hidden="true"></i> Ubuntu .deb</a>
          <a href="https://github.com/WGSExtract/gui-for-cli/releases/download/v0.6.6/WGSExtract-0.6.6-1.x86_64.rpm"><i class="bi bi-box-seam" aria-hidden="true"></i> Fedora .rpm</a>
          <a href="https://github.com/WGSExtract/gui-for-cli/releases/download/v0.6.6/wgsextract-0.6.6-1-x86_64.pkg.tar.zst"><i class="bi bi-archive" aria-hidden="true"></i> Arch package</a>
        </div>
      </article>
    </div>
    <p class="release-note">Current alpha release: <a class="inline-link" href="https://github.com/WGSExtract/gui-for-cli/releases/tag/v0.6.6">v0.6.6</a>, bundling WGSExtract CLI v0.6.6. Existing Align, Realign, DeepVariant, and SV Call actions now handle Oxford Nanopore reads; also includes native Windows setup, opt-in MultiQC reports, expanded microarray/AADR exports, and targeted Y analysis. See <a class="inline-link" href="wgsextract.html">available workflows</a> or <a class="inline-link" href="https://github.com/WGSExtract/gui-for-cli/releases">full release notes</a>.</p>
  </div>
</header>
:::

::: raw
<section id="windows-smartscreen" class="smartscreen-section">
  <div class="wrap">
    <div class="section-head smartscreen-head">
      <div>
        <p class="eyebrow"><span class="pulse" aria-hidden="true"></span> Windows install note</p>
        <h2>If SmartScreen appears, continue in two clicks.</h2>
      </div>
      <p>WGSExtract v6 Alpha is new, free, open-source software. Microsoft Defender SmartScreen can warn about newer apps until enough people have downloaded and run them. If you downloaded the installer from this project, press <strong>More info</strong>, then <strong>Run anyway</strong>.</p>
    </div>
    <div class="smartscreen-steps">
      <article class="smartscreen-step">
        <div class="kicker">Step 1</div>
        <h3>Press <strong>More info</strong>.</h3>
        <img src="assets/windows-smartscreen-step1.png" alt="Windows SmartScreen warning with the More info link visible" width="676" height="640" loading="lazy" />
      </article>
      <article class="smartscreen-step">
        <div class="kicker">Step 2</div>
        <h3>Press <strong>Run anyway</strong>.</h3>
        <img src="assets/windows-smartscreen-step2.png" alt="Windows SmartScreen warning after expanding details, showing the Run anyway button" width="675" height="634" loading="lazy" />
      </article>
    </div>
  </div>
</section>
:::

::: section
::: wrap
## Install and start

- **Windows (x64):** run the `.exe` installer, then launch WGSExtract. If SmartScreen appears, follow the steps above only for an installer downloaded from this project's releases.
- **macOS:** choose Apple Silicon or Intel, open the `.dmg`, and drag WGSExtract to Applications. The app and disk images are signed and notarized.
- **Linux (x86_64):** make the AppImage executable (`chmod +x WGSExtract_0.6.6_amd64.AppImage`) and run it, or install the `.deb`, `.rpm`, or Arch package with your distro's package manager.

On first launch, run the bundle's setup to install the pinned CLI and its runtime tools; downloads need a network connection. Windows CLI setup runs natively without WSL or MSYS2. Reference data and optional tools have separate requirements; install the resources needed for your chosen workflow before running it. Linux and Windows ARM64 desktop installers are not yet published.

Release builds support signed self-updates. macOS also offers **File > Uninstall** to remove setup, app data, and the app.
:::
:::

::: section
::: wrap
## Related rough sites

This site and the [WGSExtract CLI website](https://wgsextract.github.io/wgsextract-cli/) are rough AI-generated websites while WGSExtract v6 Alpha is still being assembled.
:::
:::

::: section
::: wrap
## More information

The download page stays intentionally small. The GUI for CLI project background, architecture notes, and documentation map live on the other pages.

{{ button: WGSExtract story|wgsextract.html|primary }}
{{ button: Architecture|architecture.html }}
{{ button: Documentation|docs.html }}
:::
:::
