# Windows Pacman Runtime Setup

`wgsextract-cli` can run required bioinformatics tools on native Windows through an MSYS2 UCRT64 runtime and selected optional tools through the host Pixi environment. The recommended normal Windows runtime is `tool_runtime = "windows"` or `WGSEXTRACT_TOOL_RUNTIME=windows`.

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

This bootstraps Pixi and MSYS2 if needed, installs the Pixi project environment, runs the pacman runtime setup helper, and persists `tool_runtime = "windows"` plus the UCRT64 bin path in the WGSExtract config file. The helper downloads prebuilt WGSExtract native release assets for tools such as BWA and minimap2 when available, so normal installs do not need a local C compiler.

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

MSYS2 UCRT64 publishes packages for the required HTS tools used by WGSExtract, including `samtools`, `bcftools`, `htslib`, `bgzip`, and `tabix`. It also provides optional pacman-runtime coverage for `curl` and `htsfile`. It does not currently publish every bioinformatics tool WGSExtract can use, so WGSExtract publishes prebuilt UCRT64 `.exe` assets for supported native build/package tools such as BWA and minimap2. The setup helper installs those binaries under:

```text
C:\msys64\ucrt64\bin
```

The release binaries are produced by the `Release Windows Native Tools` GitHub Actions workflow and packaged as `wgsextract-<tool>-<version>-windows-ucrt64.zip`. For GitHub release asset URLs, the installer verifies the ZIP against the asset's GitHub-provided SHA-256 digest, using the same release-asset digest strategy as GitHub-hosted reference genome downloads. Set `GITHUB_TOKEN` to authenticate the GitHub API lookup if needed. If the digest lookup is unavailable, the helper warns and continues without that SHA-256 check; if GitHub metadata is fetched but lacks valid SHA-256 asset metadata, or if the downloaded ZIP does not match the digest, installation fails. For local ZIPs or non-GitHub URLs, pass the matching PowerShell parameter, such as `-BwaBinarySha256` or `-Minimap2BinarySha256`.

If the release asset cannot be downloaded, or if you pass a force-build flag, the helper builds supported tools locally. That fallback build uses the MSYS2 UCRT64 toolchain installed by pacman, specifically `mingw-w64-ucrt-x86_64-gcc` plus build helpers such as `base-devel`, `make`, `curl`, and `tar`. Windows does not include a built-in C compiler suitable for these builds. Visual Studio Build Tools or the Windows SDK are optional Microsoft installs and are not used by this helper; the script runs `make CC=gcc` inside MSYS2 UCRT64 for a consistent native Windows `.exe`.

The default source for fallback builds is BWA `0.7.19` from the upstream GitHub release tag. During the build, the helper adds small Windows/UCRT64 compatibility shims for POSIX APIs that upstream BWA expects but UCRT64 does not provide. You can rebuild it explicitly with:

```powershell
.\scripts\setup_pacman_runtime.ps1 -ForceBwaBuild -SkipBwaDownload
```

You can rebuild minimap2 explicitly with:

```powershell
.\scripts\setup_pacman_runtime.ps1 -ForceMinimap2Build -SkipMinimap2Download
```

## Validation

After setup, verify the runtime:

```powershell
$env:WGSEXTRACT_TOOL_RUNTIME = "pacman"
$env:WGSEXTRACT_PACMAN_UCRT64_BIN = "C:\msys64\ucrt64\bin"
pixi run wgsextract deps pacman check
pixi run wgsextract deps check --tool bwa
$env:WGSEXTRACT_TOOL_RUNTIME = "windows"
pixi run wgsextract deps check --tool fastqc
pixi run wgsextract benchmark --runtime pacman --profile smoke --suite core --outdir out\benchmark-pacman-smoke
Remove-Item Env:\WGSEXTRACT_TOOL_RUNTIME -ErrorAction SilentlyContinue
Remove-Item Env:\WGSEXTRACT_PACMAN_UCRT64_BIN -ErrorAction SilentlyContinue
```

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
mingw-w64-ucrt-x86_64-htslib
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

The runtime resolver searches common MSYS2 locations automatically. To pin the tool directory while using the native Windows hybrid runtime, set:

```powershell
$env:WGSEXTRACT_PACMAN_UCRT64_BIN = "C:\msys64\ucrt64\bin"
$env:WGSEXTRACT_TOOL_RUNTIME = "windows"
```

Or set `pacman_ucrt64_bin` and `tool_runtime = "windows"` in the WGSExtract config file.
