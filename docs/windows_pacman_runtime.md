# Windows Pacman Runtime Setup

`wgsextract-cli` can run required bioinformatics tools on native Windows through an MSYS2 UCRT64 runtime. This mode is selected with `--runtime pacman` or `WGSEXTRACT_TOOL_RUNTIME=pacman`.

The pacman runtime is different from the bundled `cygwin` and `msys2` runtimes: it uses tools installed into a normal MSYS2 installation, usually `C:\msys64\ucrt64\bin`, and invokes those Windows `.exe` files directly.

## Prerequisites

Install MSYS2 from <https://www.msys2.org/>. The default install path, `C:\msys64`, is recommended.

The setup helper expects these MSYS2 files to exist:

```text
C:\msys64\usr\bin\bash.exe
C:\msys64\usr\bin\pacman.exe
```

## Automated Setup

Run the helper from a PowerShell terminal at the repository root:

```powershell
.\scripts\setup_pacman_runtime.ps1
```

For a non-default MSYS2 location:

```powershell
.\scripts\setup_pacman_runtime.ps1 -Msys2Root D:\tools\msys64
```

The helper installs the required UCRT64 packages with pacman, builds `bwa.exe` from source, copies it into `ucrt64\bin`, and validates the runtime with `wgsextract deps pacman check` when Pixi is available.

## Why BWA Is Built

MSYS2 UCRT64 publishes packages for the required HTS tools used by WGSExtract, including `samtools`, `bcftools`, `htslib`, `bgzip`, and `tabix`. It does not currently publish the BWA package needed by the alignment benchmark, so the setup helper compiles BWA and installs the resulting binary as:

```text
C:\msys64\ucrt64\bin\bwa.exe
```

The default source is BWA `0.7.19` from the upstream GitHub release tag. During the build, the helper adds small Windows/UCRT64 compatibility shims for POSIX APIs that upstream BWA expects but UCRT64 does not provide. You can rebuild it explicitly with:

```powershell
.\scripts\setup_pacman_runtime.ps1 -ForceBwaBuild
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

The helper installs these MSYS2 packages:

```text
base-devel
curl
git
make
tar
mingw-w64-ucrt-x86_64-bcftools
mingw-w64-ucrt-x86_64-gcc
mingw-w64-ucrt-x86_64-htslib
mingw-w64-ucrt-x86_64-samtools
mingw-w64-ucrt-x86_64-zlib
```

If you manage MSYS2 manually, install the packages above in an MSYS2 shell, then compile BWA and copy `bwa.exe` into `ucrt64\bin`.

## Configuration

The runtime resolver searches common MSYS2 locations automatically. To pin the tool directory, set one of these:

```powershell
$env:WGSEXTRACT_PACMAN_UCRT64_BIN = "C:\msys64\ucrt64\bin"
$env:WGSEXTRACT_TOOL_RUNTIME = "pacman"
```

Or set `pacman_ucrt64_bin` and `tool_runtime = "pacman"` in the WGSExtract config file.