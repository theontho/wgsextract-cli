# Windows Pacman Runtime Setup

`wgsextract-cli` can run required bioinformatics tools on native Windows through an MSYS2 UCRT64 runtime and selected optional tools through the host Pixi environment. The recommended runtime is `tool_runtime = "windows"` in `config.toml`, which `install_windows.bat` sets for you.

```toml
tool_runtime = "windows"
pacman_ucrt64_bin = "C:/msys64/ucrt64/bin"   # optional; common MSYS2 locations are searched automatically
```

Run `pixi run wgsextract config` to see the config file path and current values. Installers and CI can override these settings for one process with the `WGSEXTRACT_TOOL_RUNTIME` and `WGSEXTRACT_PACMAN_UCRT64_BIN` environment variables, which take precedence over `config.toml`.

The strict `pacman` runtime is different from the bundled `cygwin`, bundled `msys2`, and WSL2 runtimes: it uses tools installed into a normal MSYS2 installation, usually `C:\msys64\ucrt64\bin`, and invokes those Windows `.exe` files directly. The `windows` runtime keeps that native pacman behavior but also allows optional tools from host Pixi, and never falls back to WSL.

## Prerequisites

The standard `install_windows.bat` bootstrapper installs Pixi and MSYS2 when they are missing. If you manage MSYS2 yourself, install it from <https://www.msys2.org/>. The default install path, `C:\msys64`, is recommended.

The setup helper expects these MSYS2 files to exist:

```text
C:\msys64\usr\bin\bash.exe
C:\msys64\usr\bin\pacman.exe
```

## Automated Setup

For the standard Windows setup, run the recommended batch installer from the repository root:

```bat
install_windows.bat
```

This bootstraps Pixi and MSYS2 if needed, installs the Pixi project environment, runs the pacman runtime setup helper, and persists `tool_runtime = "windows"` plus the UCRT64 bin path in the WGSExtract config file. The helper downloads prebuilt WGSExtract native release assets for tools such as BWA, minimap2, samblaster, and fastp when available, so normal installs do not need a local C compiler.

If you run a standalone copy of `install_windows.bat` without the rest of the repository, place it in an empty directory. The source bootstrap refuses to copy into a non-empty directory unless you explicitly pass `--allow-nonempty-bootstrap-dir`.

For locked-down automation, you can override prerequisite download locations with `WGSEXTRACT_PIXI_INSTALL_URL` and `WGSEXTRACT_MSYS2_INSTALLER_URL`. Set `WGSEXTRACT_PIXI_INSTALL_SHA256` or `WGSEXTRACT_MSYS2_INSTALLER_SHA256` to a 64-character SHA-256 digest to verify those downloaded installers before they run.

For a non-default MSYS2 location:

```bat
install_windows.bat --msys2-root D:\tools\msys64
```

To force a local BWA build instead of using the release binary:

```bat
install_windows.bat --force-bwa-build --skip-bwa-download
```

To test or pin a specific BWA ZIP asset:

```bat
install_windows.bat --bwa-binary-url https://github.com/theontho/wgsextract-cli/releases/download/v0.1.0/wgsextract-bwa-0.7.19-windows-ucrt64.zip
```

For local ZIPs or non-GitHub URLs, pass the expected digest explicitly:

```bat
install_windows.bat --bwa-binary-url D:\tools\wgsextract-bwa-0.7.19-windows-ucrt64.zip --bwa-binary-sha256 <sha256>
```

The minimap2 release asset can be overridden the same way:

```bat
install_windows.bat --minimap2-binary-url D:\tools\wgsextract-minimap2-2.30-windows-ucrt64.zip --minimap2-binary-sha256 <sha256>
```

Native samblaster and fastp assets also support the same override pattern:

```bat
install_windows.bat --samblaster-binary-url D:\tools\wgsextract-samblaster-0.1.26-windows-ucrt64.zip --samblaster-binary-sha256 <sha256>
install_windows.bat --fastp-binary-url D:\tools\wgsextract-fastp-0.24.1-windows-ucrt64.zip --fastp-binary-sha256 <sha256>
```

To remove the local project install and clear those pacman config defaults:

```bat
uninstall_windows.bat
```

By default, the uninstaller does not remove Pixi, MSYS2, or MSYS2 packages because those may be shared with other projects. To remove the bootstrapper-installed prerequisites too, opt in explicitly:

```bat
uninstall_windows.bat --yes --remove-prerequisites
```

## PowerShell Runtime Helper

Run the helper from a PowerShell terminal at the repository root:

```powershell
.\scripts\setup_pacman_runtime.ps1
```

For a non-default MSYS2 location:

```powershell
.\scripts\setup_pacman_runtime.ps1 -Msys2Root D:\tools\msys64
```

The helper installs the required UCRT64 runtime packages with pacman, downloads prebuilt WGSExtract native release assets when available, copies executables into `ucrt64\bin`, and validates the strict pacman runtime with `wgsextract deps pacman check` when Pixi is available. If a prebuilt binary is unavailable, it falls back to a local MSYS2 UCRT64 build when that tool has a native build path.

## Runtime modes

Use `windows` for normal native Windows operation. It searches:

1. normal host `PATH`;
2. MSYS2 UCRT64 pacman paths;
3. host Pixi environments.

It does not fall back to WSL, so a machine with WSL installed will still use native Windows tools. Use `pacman` only when you want a strict diagnostic mode that ignores host Pixi optional tools.

## Why Some Tools Are Built

MSYS2 UCRT64 publishes packages for the required HTS tools used by WGSExtract, including `samtools`, `bcftools`, `htslib`, `bgzip`, and `tabix`. It also provides optional pacman-runtime coverage for `curl` and `htsfile`. It does not currently publish every bioinformatics tool WGSExtract can use, so WGSExtract publishes prebuilt UCRT64 `.exe` assets for supported native build/package tools such as BWA, minimap2, samblaster, and fastp. The setup helper installs those binaries under:

```text
C:\msys64\ucrt64\bin
```

The release binaries are produced by the `Release Windows Native Tools` GitHub Actions workflow and packaged as `wgsextract-<tool>-<version>-windows-ucrt64.zip`. GitHub release asset URLs are verified against GitHub's SHA-256 asset digest, as described in [download verification](https://theontho.github.io/wgsextract-cli/install.html#checksums). For local ZIPs or non-GitHub URLs, pass the matching digest: `--bwa-binary-sha256` (or `WGSEXTRACT_BWA_BINARY_SHA256`) to the installer, or `-BwaBinarySha256`, `-Minimap2BinarySha256`, `-SamblasterBinarySha256`, or `-FastpBinarySha256` to the PowerShell helper.

If the release asset cannot be downloaded, or if you pass a force-build flag, the helper builds supported tools locally. That fallback build uses the MSYS2 UCRT64 toolchain installed by pacman, specifically `mingw-w64-ucrt-x86_64-gcc` plus build helpers such as `base-devel`, `make`, `curl`, and `tar`. Windows does not include a built-in C compiler suitable for these builds. Visual Studio Build Tools or the Windows SDK are optional Microsoft installs and are not used by this helper; the script runs `make CC=gcc` inside MSYS2 UCRT64 for a consistent native Windows `.exe`.

The default source for fallback builds is BWA `0.7.19` from the upstream GitHub release tag. During the build, the helper adds small Windows/UCRT64 compatibility shims for POSIX APIs that upstream BWA expects but UCRT64 does not provide. You can rebuild it explicitly with:

```powershell
.\scripts\setup_pacman_runtime.ps1 -ForceBwaBuild -SkipBwaDownload
```

You can rebuild minimap2 explicitly with:

```powershell
.\scripts\setup_pacman_runtime.ps1 -ForceMinimap2Build -SkipMinimap2Download
```

You can rebuild samblaster and fastp explicitly with:

```powershell
.\scripts\setup_pacman_runtime.ps1 -ForceSamblasterBuild -SkipSamblasterDownload
.\scripts\setup_pacman_runtime.ps1 -ForceFastpBuild -SkipFastpDownload
```

## Optional tool coverage

Native Windows sources for the optional tools that `deps check` reports. The preferred order is MSYS2 pacman packages, then native Pixi packages, then WGS Extract UCRT64 release assets; WSL is not used.

| Tool | Native Windows source | Notes |
| --- | --- | --- |
| `curl`, `htsfile` | MSYS2 pacman | `htsfile` comes with `mingw-w64-ucrt-x86_64-htslib`, which mandatory tools already need. |
| `java`, `gatk` | Pixi (`openjdk`, `gatk4`) | Part of the default Pixi environment. |
| `fastqc`, `haplogrep` | Pixi (bioconda noarch) | Java-based. |
| `yleaf` | Pixi (PyPI) | Installed from the upstream Yleaf `v4.1.6` release archive (SHA-256 pinned in `pixi.toml` and `pixi.lock`), because the bioconda package lacks Yleaf's marker data. |
| `bwa`, `minimap2` | WGS Extract release asset | Built with MSYS2 UCRT64; local build fallback available. |
| `samblaster` | WGS Extract release asset | Built from `v.0.1.26` with small Windows shims for POSIX allocation/timing helpers. |
| `fastp` | WGS Extract release asset | Built from `v0.24.1` against MSYS2 UCRT64 `isa-l`, `libdeflate`, `zlib`, and GCC runtime packages. Patched for >2 GB inputs, 64-bit report counters, and JSON-safe command paths; tested on paired ~20 GB FASTQs. |
| `freebayes` | Not available | Larger C++ dependency stack. Candidate for a future release asset. |
| `delly` | Not available | Larger C++/HTS stack. Candidate after freebayes. |
| `sambamba` | Not available | D/LDC build; likely harder to keep small and reproducible. |
| `pbmm2`, `pbsv` | Not available | PacBio stack; the `pacbio` Pixi environment has no `win-64` packages. |
| `sniffles` | Not available | The native Pixi solve fails on `win-64`. |
| `vep` | Not available | Perl/cache stack; the `vep` Pixi environment is macOS/Linux only. |
| `run_deepvariant`, `dv_call_variants.py` | Not available | DeepVariant is Linux/container oriented. |

## Validation

After setup, verify the runtime:

```powershell
pixi run wgsextract deps pacman check
pixi run wgsextract deps check
pixi run wgsextract deps check --tool bwa
pixi run wgsextract deps check --tool fastqc
pixi run wgsextract benchmark --runtime pacman --profile smoke --suite core --outdir out\benchmark-pacman-smoke
```

`deps check` exits 1 when a mandatory tool is missing, and `deps check --tool <name>` exits 1 when that tool is missing. With `tool_runtime = "windows"`, tools resolve from pacman paths and host Pixi. To prove that a tool comes from MSYS2/pacman only, use `deps pacman check`, or temporarily set `WGSEXTRACT_TOOL_RUNTIME=pacman` for that shell.

For the default benchmark suite used in runtime comparisons:

```powershell
pixi run wgsextract benchmark --runtime pacman --outdir out\benchmark-pacman-default
```

## Manual Package List

For normal prebuilt native-tool installs, the helper installs these MSYS2 packages:

```text
gzip
tar
mingw-w64-ucrt-x86_64-bcftools
mingw-w64-ucrt-x86_64-curl
mingw-w64-ucrt-x86_64-gcc-libs
mingw-w64-ucrt-x86_64-htslib
mingw-w64-ucrt-x86_64-isa-l
mingw-w64-ucrt-x86_64-libdeflate
mingw-w64-ucrt-x86_64-samtools
mingw-w64-ucrt-x86_64-zlib
```

Fallback local native builds additionally install:

```text
base-devel
curl
git
make
mingw-w64-ucrt-x86_64-gcc
mingw-w64-ucrt-x86_64-zlib
```

If you manage MSYS2 manually, install the packages above in an MSYS2 UCRT64 shell, then compile supported native tools with GCC and copy their `.exe` files into `ucrt64\bin`.

## Configuration

The runtime resolver searches common MSYS2 locations automatically. To pin the tool directory, set `pacman_ucrt64_bin` (alias `pacman_bin`) next to `tool_runtime` in `config.toml`, as shown at the top of this page. Valid `tool_runtime` values are `auto`, `native`, `windows`, `pacman`, `msys2`, `cygwin`, and `wsl`; `runtime_directory` sets where the bundled `msys2`/`cygwin` runtimes live.

The environment variables `WGSEXTRACT_TOOL_RUNTIME`, `WGSEXTRACT_PACMAN_UCRT64_BIN`, and `WGSEXTRACT_RUNTIME_DIR` override the matching settings for a single process. They exist for installers, CI, and diagnostics; use `config.toml` for persistent setup.
