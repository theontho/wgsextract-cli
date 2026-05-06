# Windows Pacman Runtime Setup

`wgsextract-cli` can run required bioinformatics tools on native Windows through an MSYS2 UCRT64 runtime. This is the recommended Windows runtime and is selected with `--runtime pacman` or `WGSEXTRACT_TOOL_RUNTIME=pacman`.

The pacman runtime is different from the bundled `cygwin`, bundled `msys2`, and WSL2 runtimes: it uses tools installed into a normal MSYS2 installation, usually `C:\msys64\ucrt64\bin`, and invokes those Windows `.exe` files directly.

## Prerequisites

Install MSYS2 from <https://www.msys2.org/>. The default install path, `C:\msys64`, is recommended.

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

This installs the Pixi project environment, runs the pacman runtime setup helper, and persists `tool_runtime = "pacman"` plus the UCRT64 bin path in the WGSExtract config file. The helper downloads the prebuilt WGSExtract BWA release asset when available, so normal installs do not need a local C compiler.

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

To remove the local project install and clear those pacman config defaults:

```bat
uninstall_windows.bat
```

The uninstaller does not remove Pixi, MSYS2, or MSYS2 packages because those may be shared with other projects.

## PowerShell Runtime Helper

Run the helper from a PowerShell terminal at the repository root:

```powershell
.\scripts\setup_pacman_runtime.ps1
```

For a non-default MSYS2 location:

```powershell
.\scripts\setup_pacman_runtime.ps1 -Msys2Root D:\tools\msys64
```

The helper installs the required UCRT64 runtime packages with pacman, downloads the prebuilt WGSExtract BWA release asset when available, copies `bwa.exe` into `ucrt64\bin`, and validates the runtime with `wgsextract deps pacman check` when Pixi is available. If the prebuilt binary is unavailable, it falls back to building BWA locally.

## Why BWA Is Built

MSYS2 UCRT64 publishes packages for the required HTS tools used by WGSExtract, including `samtools`, `bcftools`, `htslib`, `bgzip`, and `tabix`. It does not currently publish the BWA package needed by the alignment benchmark, so WGSExtract publishes a prebuilt UCRT64 `bwa.exe` on GitHub Releases. The setup helper installs that binary as:

```text
C:\msys64\ucrt64\bin\bwa.exe
```

The release binary is produced by the `Release Windows BWA` GitHub Actions workflow and packaged as `wgsextract-bwa-<version>-windows-ucrt64.zip`. For GitHub release asset URLs, the installer verifies the ZIP against the asset's GitHub-provided SHA-256 digest. For local ZIPs or non-GitHub URLs, set `WGSEXTRACT_BWA_BINARY_SHA256` when you want checksum verification.

If the release asset cannot be downloaded, or if you pass `-ForceBwaBuild`, the helper builds BWA locally. That fallback build uses the MSYS2 UCRT64 toolchain installed by pacman, specifically `mingw-w64-ucrt-x86_64-gcc` plus `base-devel`, `make`, `curl`, and `tar`. Windows does not include a built-in C compiler suitable for this build. Visual Studio Build Tools or the Windows SDK are optional Microsoft installs and are not used by this helper; the script runs `make CC=gcc` inside MSYS2 UCRT64 for a consistent native Windows `.exe`.

The default source for fallback builds is BWA `0.7.19` from the upstream GitHub release tag. During the build, the helper adds small Windows/UCRT64 compatibility shims for POSIX APIs that upstream BWA expects but UCRT64 does not provide. You can rebuild it explicitly with:

```powershell
.\scripts\setup_pacman_runtime.ps1 -ForceBwaBuild -SkipBwaDownload
```

## Validation

After setup, verify the runtime:

```powershell
$env:WGSEXTRACT_TOOL_RUNTIME = "pacman"
$env:WGSEXTRACT_PACMAN_UCRT64_BIN = "C:\msys64\ucrt64\bin"
pixi run wgsextract deps pacman check
pixi run wgsextract deps check --tool bwa
pixi run wgsextract benchmark --runtime pacman --profile smoke --suite core --outdir out\benchmark-pacman-smoke
Remove-Item Env:\WGSEXTRACT_TOOL_RUNTIME -ErrorAction SilentlyContinue
Remove-Item Env:\WGSEXTRACT_PACMAN_UCRT64_BIN -ErrorAction SilentlyContinue
```

For the default benchmark suite used in runtime comparisons:

```powershell
pixi run wgsextract benchmark --runtime pacman --outdir out\benchmark-pacman-default
```

## Manual Package List

For normal prebuilt-BWA installs, the helper installs these MSYS2 packages:

```text
gzip
tar
mingw-w64-ucrt-x86_64-bcftools
mingw-w64-ucrt-x86_64-htslib
mingw-w64-ucrt-x86_64-samtools
mingw-w64-ucrt-x86_64-zlib
```

Fallback local BWA builds additionally install:

```text
base-devel
curl
git
make
mingw-w64-ucrt-x86_64-gcc
```

If you manage MSYS2 manually, install the packages above in an MSYS2 UCRT64 shell, then compile BWA with GCC and copy `bwa.exe` into `ucrt64\bin`.

## Configuration

The runtime resolver searches common MSYS2 locations automatically. To pin the tool directory, set one of these:

```powershell
$env:WGSEXTRACT_PACMAN_UCRT64_BIN = "C:\msys64\ucrt64\bin"
$env:WGSEXTRACT_TOOL_RUNTIME = "pacman"
```

Or set `pacman_ucrt64_bin` and `tool_runtime = "pacman"` in the WGSExtract config file.
