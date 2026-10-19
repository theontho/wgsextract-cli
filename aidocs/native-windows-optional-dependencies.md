# Native Windows optional dependency plan

## Goal

Improve native Windows optional dependency coverage without making WSL the
normal path. WSL remains useful for tools that are effectively Linux-only, but
large WGS inputs stored on the Windows filesystem pay a major `/mnt/c` I/O
penalty, so the preferred order is:

1. MSYS2 UCRT64 pacman packages.
2. Native Windows Pixi packages that solve and run without WSL.
3. WGS Extract release assets built for MSYS2 UCRT64 (BWA, minimap2,
   samblaster, fastp).
4. WSL or container-only fallback, deferred until native options are exhausted.

## Current status

The per-tool inventory of `wgsextract_cli.core.dependencies.OPTIONAL_TOOLS`
on native Windows, including which tools come from pacman, Pixi, or WGS
Extract release assets, is maintained in
[docs/windows_pacman_runtime.md](../docs/windows_pacman_runtime.md#optional-tool-coverage).
Keep that table as the single source of truth.

Notes that are not in that table:

- `yleaf` is a PyPI git dependency (upstream Yleaf `v4.1.2`) in the
  `bio-tools` Pixi feature, not the bioconda package, because bioconda's
  artifact omits Yleaf's marker data. It is part of the default `win-64`
  environment.
- `pbsv` only has a `linux-64` artifact. Do not advertise native Windows
  support unless a fresh `win-64` solve and smoke test succeeds.
- `scripts/setup_pacman_runtime.ps1` installs the optional pacman packages
  (`curl`, `htslib` for `htsfile`) explicitly so its output shows which
  optional tools pacman covers.

## Remaining native release assets

Follow the existing BWA/minimap2/samblaster/fastp pattern:

- build in GitHub Actions on `windows-latest` with MSYS2 UCRT64;
- package only the runtime executable and required adjacent files;
- upload ZIP assets to WGS Extract CLI releases;
- have `scripts/setup_pacman_runtime.ps1` download, verify GitHub release asset
  SHA-256 metadata, and install into the UCRT64 bin tree;
- keep `-Skip...Download`, `-Skip...Build`, and `-Force...Build` style switches
  for installer/debug control.

Recommended order for the tools still missing:

1. `freebayes`
2. `delly`
3. `sambamba`
4. `pbmm2`

`sniffles`, `vep`, and DeepVariant stay deferred (see the table for reasons).

## Runtime mode

The Windows installer persists `tool_runtime = "windows"` in `config.toml`.
That mode searches normal host `PATH`, MSYS2 UCRT64 pacman paths, then host
Pixi environments, and never falls back to WSL. It is the normal native
Windows mode for GUI and standalone installer use.

Strict `tool_runtime = "pacman"` remains available for diagnostics and setup
validation when the caller wants to prove that a tool comes from MSYS2/pacman
only. `tool_runtime = "auto"` can still use WSL on machines where WSL is
installed, so native Windows setup should use `windows` rather than `auto`.
