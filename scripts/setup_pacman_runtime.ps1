[CmdletBinding()]
param(
    [string]$Msys2Root = $(if ($env:MSYS2_ROOT) { $env:MSYS2_ROOT } else { "C:\msys64" }),
    [string]$BwaVersion = "0.7.19",
    [string]$BuildDir = "tmp\pacman-runtime-build",
    [switch]$ForceBwaBuild,
    [switch]$SkipBwaBuild,
    [switch]$SkipPackageInstall
)

$ErrorActionPreference = "Stop"

function Resolve-RepoRelativePath {
    param([string]$Path)

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }

    $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Path))
}

function ConvertTo-MsysPath {
    param([string]$WindowsPath)

    $escapedPath = $WindowsPath.Replace("'", "'\''")
    $converted = & $script:BashPath -lc "cygpath -u '$escapedPath'"
    if ($LASTEXITCODE -ne 0 -or -not $converted) {
        throw "Failed to convert path to MSYS2 format: $WindowsPath"
    }
    return $converted.Trim()
}

function Invoke-Msys2Script {
    param([string]$ScriptText)

    $tempScript = Join-Path $env:TEMP ("wgsextract-pacman-{0}.sh" -f ([guid]::NewGuid()))
    $scriptBody = @"
set -euo pipefail
export MSYSTEM=UCRT64
export CHERE_INVOKING=1
export PATH=/ucrt64/bin:/usr/bin:/bin:`$PATH
$ScriptText
"@

    Set-Content -Path $tempScript -Value $scriptBody -Encoding ASCII
    try {
        $msysScript = ConvertTo-MsysPath $tempScript
        & $script:BashPath -lc "bash '$msysScript'"
        if ($LASTEXITCODE -ne 0) {
            throw "MSYS2 command failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Remove-Item $tempScript -Force -ErrorAction SilentlyContinue
    }
}

$Msys2Root = [System.IO.Path]::GetFullPath($Msys2Root)
$script:BashPath = Join-Path $Msys2Root "usr\bin\bash.exe"
$pacmanPath = Join-Path $Msys2Root "usr\bin\pacman.exe"
$ucrt64Bin = Join-Path $Msys2Root "ucrt64\bin"

if (-not (Test-Path $script:BashPath)) {
    throw "MSYS2 bash was not found at $script:BashPath. Install MSYS2 first."
}
if (-not (Test-Path $pacmanPath)) {
    throw "MSYS2 pacman was not found at $pacmanPath. Install MSYS2 first."
}

if (-not $SkipPackageInstall) {
    $packages = @(
        "base-devel",
        "curl",
        "git",
        "make",
        "tar",
        "mingw-w64-ucrt-x86_64-bcftools",
        "mingw-w64-ucrt-x86_64-gcc",
        "mingw-w64-ucrt-x86_64-htslib",
        "mingw-w64-ucrt-x86_64-samtools",
        "mingw-w64-ucrt-x86_64-zlib"
    )
    Write-Host "Installing MSYS2 UCRT64 packages..."
    Invoke-Msys2Script ("pacman -Sy --needed --noconfirm " + ($packages -join " "))
}
else {
    Write-Host "Skipping MSYS2 package installation."
}

$bwaPath = Join-Path $ucrt64Bin "bwa.exe"
$shouldBuildBwa = (-not $SkipBwaBuild) -and ($ForceBwaBuild -or -not (Test-Path $bwaPath))

if ($shouldBuildBwa) {
    $buildRoot = Resolve-RepoRelativePath $BuildDir
    New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
    $buildRootMsys = ConvertTo-MsysPath $buildRoot
    $archiveName = "v$BwaVersion.tar.gz"
    $sourceDir = "bwa-$BwaVersion"
    $sourceUrl = "https://github.com/lh3/bwa/archive/refs/tags/v$BwaVersion.tar.gz"

    Write-Host "Building BWA $BwaVersion for MSYS2 UCRT64..."
    Invoke-Msys2Script @"
mkdir -p '$buildRootMsys'
cd '$buildRootMsys'
rm -rf '$sourceDir' '$archiveName'
curl -L --retry 3 -o '$archiveName' '$sourceUrl'
tar -xzf '$archiveName'
cd '$sourceDir'
mkdir -p sys
cat > sys/resource.h <<'EOF'
#ifndef WGSEXTRACT_BWA_RESOURCE_H
#define WGSEXTRACT_BWA_RESOURCE_H
#include <string.h>
#include <sys/time.h>
#ifndef RUSAGE_SELF
#define RUSAGE_SELF 0
#endif
struct rusage {
    struct timeval ru_utime;
    struct timeval ru_stime;
    long ru_maxrss;
};
static inline int getrusage(int who, struct rusage *usage) {
    (void)who;
    memset(usage, 0, sizeof(*usage));
    return 0;
}
#endif
EOF
cat > sys/mman.h <<'EOF'
#ifndef WGSEXTRACT_BWA_MMAN_H
#define WGSEXTRACT_BWA_MMAN_H
#include <stddef.h>
#define PROT_READ 1
#define PROT_WRITE 2
#define MAP_SHARED 1
#define MAP_FAILED ((void *)-1)
static inline int shm_open(const char *name, int oflag, int mode) {
    (void)name;
    (void)oflag;
    (void)mode;
    return -1;
}
static inline int shm_unlink(const char *name) {
    (void)name;
    return -1;
}
static inline void *mmap(void *addr, size_t length, int prot, int flags, int fd, long offset) {
    (void)addr;
    (void)length;
    (void)prot;
    (void)flags;
    (void)fd;
    (void)offset;
    return MAP_FAILED;
}
static inline int munmap(void *addr, size_t length) {
    (void)addr;
    (void)length;
    return 0;
}
#endif
EOF
cat > sys/wait.h <<'EOF'
#ifndef WGSEXTRACT_BWA_WAIT_H
#define WGSEXTRACT_BWA_WAIT_H
#ifndef WNOHANG
#define WNOHANG 1
#endif
static inline int waitpid(int pid, int *status, int options) {
    (void)pid;
    (void)status;
    (void)options;
    return -1;
}
#endif
EOF
cat > win_compat.h <<'EOF'
#ifndef WGSEXTRACT_BWA_WIN_COMPAT_H
#define WGSEXTRACT_BWA_WIN_COMPAT_H
#ifdef _WIN32
#include <io.h>
#include <stdlib.h>
#include <string.h>
#ifndef fsync
#define fsync _commit
#endif
#ifndef index
#define index strchr
#endif
static inline long wgsextract_lrand48(void) {
    return rand();
}
static inline void wgsextract_srand48(long seed) {
    srand((unsigned int)seed);
}
static inline double wgsextract_drand48(void) {
    return (double)rand() / ((double)RAND_MAX + 1.0);
}
#ifndef lrand48
#define lrand48 wgsextract_lrand48
#endif
#ifndef srand48
#define srand48 wgsextract_srand48
#endif
#ifndef drand48
#define drand48 wgsextract_drand48
#endif
#ifndef http_open
#define http_open(fn) (-1)
#endif
#ifndef ftp_open
#define ftp_open(fn) (-1)
#endif
#ifndef pipe
#define pipe(pfd) (-1)
#endif
#ifndef vfork
#define vfork() (-1)
#endif
#ifndef kill
#define kill(pid, sig) (-1)
#endif
#endif
#endif
EOF
sed -i 's/^INCLUDES=[[:space:]]*$/INCLUDES= -I. -include win_compat.h/' Makefile
make clean >/dev/null 2>&1 || true
make CC=gcc
if [ -f bwa.exe ]; then
    built_bwa=bwa.exe
else
    built_bwa=bwa
fi
install -m 755 "`$built_bwa" /ucrt64/bin/bwa.exe
/ucrt64/bin/bwa.exe 2>&1 | head -8 || true
"@
}
elseif ($SkipBwaBuild) {
    Write-Host "Skipping BWA build."
}
else {
    Write-Host "BWA already exists at $bwaPath. Use -ForceBwaBuild to rebuild."
}

$requiredTools = @("samtools", "bcftools", "bgzip", "tabix", "bwa")
$missingTools = @()
foreach ($tool in $requiredTools) {
    $toolPath = Join-Path $ucrt64Bin "$tool.exe"
    if (-not (Test-Path $toolPath)) {
        $missingTools += $tool
    }
}

if ($missingTools.Count -gt 0) {
    throw "Missing pacman runtime tool(s): $($missingTools -join ', ')"
}

Write-Host "Pacman runtime tools are present in $ucrt64Bin"

$pixiCommand = Get-Command pixi -ErrorAction SilentlyContinue
if ($pixiCommand) {
    Write-Host "Running wgsextract pacman dependency check..."
    $previousRuntime = $env:WGSEXTRACT_TOOL_RUNTIME
    $previousBin = $env:WGSEXTRACT_PACMAN_UCRT64_BIN
    $env:WGSEXTRACT_TOOL_RUNTIME = "pacman"
    $env:WGSEXTRACT_PACMAN_UCRT64_BIN = $ucrt64Bin
    try {
        pixi run wgsextract deps pacman check
        if ($LASTEXITCODE -ne 0) {
            throw "wgsextract deps pacman check failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        if ($null -eq $previousRuntime) {
            Remove-Item Env:\WGSEXTRACT_TOOL_RUNTIME -ErrorAction SilentlyContinue
        }
        else {
            $env:WGSEXTRACT_TOOL_RUNTIME = $previousRuntime
        }
        if ($null -eq $previousBin) {
            Remove-Item Env:\WGSEXTRACT_PACMAN_UCRT64_BIN -ErrorAction SilentlyContinue
        }
        else {
            $env:WGSEXTRACT_PACMAN_UCRT64_BIN = $previousBin
        }
    }
}
else {
    Write-Host "Pixi is not on PATH; skipping wgsextract validation."
}

Write-Host "Pacman runtime setup complete."
Write-Host "Set WGSEXTRACT_TOOL_RUNTIME=pacman to use this runtime."
Write-Host "UCRT64 bin: $ucrt64Bin"