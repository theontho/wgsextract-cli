[CmdletBinding()]
param(
    [string]$Msys2Root = $(if ($env:MSYS2_ROOT) { $env:MSYS2_ROOT } else { "C:\msys64" }),
    [string]$BwaVersion = "0.7.19",
    [string]$Minimap2Version = "2.30",
    [string]$SamblasterVersion = "0.1.26",
    [string]$FastpVersion = "0.24.1",
    [string]$ReleaseTag = $(if ($env:WGSEXTRACT_RELEASE_TAG) { $env:WGSEXTRACT_RELEASE_TAG } else { "latest" }),
    [string]$BuildDir = "tmp\pacman-runtime-build",
    [string]$BwaBinaryUrl = $(if ($env:WGSEXTRACT_BWA_BINARY_URL) { $env:WGSEXTRACT_BWA_BINARY_URL } else { "" }),
    [string]$BwaBinarySha256 = $(if ($env:WGSEXTRACT_BWA_BINARY_SHA256) { $env:WGSEXTRACT_BWA_BINARY_SHA256 } else { "" }),
    [string]$Minimap2BinaryUrl = "",
    [string]$Minimap2BinarySha256 = "",
    [string]$SamblasterBinaryUrl = "",
    [string]$SamblasterBinarySha256 = "",
    [string]$FastpBinaryUrl = "",
    [string]$FastpBinarySha256 = "",
    [switch]$ForceBwaBuild,
    [switch]$ForceMinimap2Build,
    [switch]$ForceSamblasterBuild,
    [switch]$ForceFastpBuild,
    [switch]$SkipBwaBuild,
    [switch]$SkipMinimap2Build,
    [switch]$SkipSamblasterBuild,
    [switch]$SkipFastpBuild,
    [switch]$SkipBwaDownload,
    [switch]$SkipMinimap2Download,
    [switch]$SkipSamblasterDownload,
    [switch]$SkipFastpDownload,
    [switch]$SkipPackageInstall
)

$ErrorActionPreference = "Stop"

function Assert-SafeReleaseValue {
    param(
        [string]$Name,
        [string]$Value,
        [switch]$AllowEmpty
    )

    if (-not $Value) {
        if ($AllowEmpty) {
            return
        }
        throw "$Name is required."
    }
    if ($Value -notmatch '^[A-Za-z0-9._-]+$') {
        throw "$Name contains unsupported characters. Use only letters, digits, '.', '_' or '-'."
    }
}

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

    $convertedLines = @($converted) | ForEach-Object { "$_".Trim() } | Where-Object { $_ }
    $pathLine = $convertedLines | Where-Object { $_.StartsWith("/") } | Select-Object -Last 1
    if (-not $pathLine) {
        $pathLine = $convertedLines | Select-Object -Last 1
    }
    return $pathLine
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

function Initialize-PacmanKeyring {
    # Fresh MSYS2 installs need an explicit `pacman-key --init` and
    # `pacman-key --populate msys2` before any signed `pacman -Sy` operation
    # will succeed. The first-time bash init can leave the keyring
    # partially populated (no secret key) and a stale `/var/lib/pacman/db.lck`,
    # causing `error: failed to synchronize all databases (unable to lock database)`.
    $gnupgRoot = Join-Path $Msys2Root "etc\pacman.d\gnupg"
    $marker = Join-Path $gnupgRoot ".wgsextract-keyring-ready"
    if (Test-Path $marker) { return }

    Write-Host "Triggering MSYS2 first-time initialization..."
    try { & $script:BashPath -lc "true" 2>&1 | Out-Host } catch { }

    Start-Sleep -Seconds 2
    $dbLock = Join-Path $Msys2Root "var\lib\pacman\db.lck"
    if (Test-Path $dbLock) {
        Write-Host "Removing stale MSYS2 pacman db lock: $dbLock"
        Remove-Item -LiteralPath $dbLock -Force -ErrorAction SilentlyContinue
    }

    Write-Host "Initializing MSYS2 pacman keyring..."
    if (Test-Path $gnupgRoot) {
        try { Remove-Item -LiteralPath $gnupgRoot -Recurse -Force -ErrorAction Stop } catch { }
    }
    Invoke-Msys2Script "pacman-key --init && pacman-key --populate msys2"

    New-Item -ItemType Directory -Force -Path (Split-Path $marker -Parent) | Out-Null
    Set-Content -LiteralPath $marker -Value (Get-Date).ToString('o') -Encoding ASCII
}

function Invoke-PacmanCommand {
    param([string]$ScriptText)

    $dbLock = Join-Path $Msys2Root "var\lib\pacman\db.lck"
    $attempts = 3
    for ($i = 1; $i -le $attempts; $i += 1) {
        if (Test-Path $dbLock) {
            Write-Host "Removing stale MSYS2 pacman db lock before retry: $dbLock"
            Remove-Item -LiteralPath $dbLock -Force -ErrorAction SilentlyContinue
        }
        try {
            Invoke-Msys2Script $ScriptText
            return
        }
        catch {
            if ($i -eq $attempts) { throw }
            $delay = 5 * $i
            Write-Host "pacman command failed (attempt $i/$attempts). Retrying in $delay s..."
            Start-Sleep -Seconds $delay
        }
    }
}

function Install-PacmanPackages {
    param(
        [string]$Description,
        [string[]]$Packages
    )

    if ($Packages.Count -eq 0) {
        return
    }

    Write-Host $Description
    Initialize-PacmanKeyring
    Invoke-PacmanCommand ("pacman -Sy --needed --noconfirm " + ($Packages -join " "))
}

function Copy-UrlOrFile {
    param(
        [string]$Source,
        [string]$Destination
    )

    if (Test-Path $Source) {
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
        return
    }

    $invokeParams = @{
        Uri = $Source
        OutFile = $Destination
    }
    if ((Get-Command Invoke-WebRequest).Parameters.ContainsKey("UseBasicParsing")) {
        $invokeParams.UseBasicParsing = $true
    }
    Invoke-WebRequest @invokeParams
}

function Resolve-GitHubReleaseAssetSha256 {
    param([string]$BinaryUrl)

    try {
        $uri = [System.Uri]$BinaryUrl
    }
    catch {
        return ""
    }

    if ($uri.Host -ne "github.com") {
        return ""
    }

    $path = $uri.AbsolutePath.Trim("/")
    $taggedMatch = [regex]::Match($path, "^([^/]+)/([^/]+)/releases/download/([^/]+)/([^/]+)$")
    $latestMatch = [regex]::Match($path, "^([^/]+)/([^/]+)/releases/latest/download/([^/]+)$")

    if ($taggedMatch.Success) {
        $owner = [System.Uri]::UnescapeDataString($taggedMatch.Groups[1].Value)
        $repo = [System.Uri]::UnescapeDataString($taggedMatch.Groups[2].Value)
        $tag = [System.Uri]::UnescapeDataString($taggedMatch.Groups[3].Value)
        $assetName = [System.Uri]::UnescapeDataString($taggedMatch.Groups[4].Value)
        $apiUrl = "https://api.github.com/repos/$owner/$repo/releases/tags/$tag"
    }
    elseif ($latestMatch.Success) {
        $owner = [System.Uri]::UnescapeDataString($latestMatch.Groups[1].Value)
        $repo = [System.Uri]::UnescapeDataString($latestMatch.Groups[2].Value)
        $assetName = [System.Uri]::UnescapeDataString($latestMatch.Groups[3].Value)
        $apiUrl = "https://api.github.com/repos/$owner/$repo/releases/latest"
    }
    else {
        return ""
    }

    $headers = @{
        Accept = "application/vnd.github+json"
        "X-GitHub-Api-Version" = "2022-11-28"
    }
    if ($env:GITHUB_TOKEN) {
        $headers["Authorization"] = "Bearer $env:GITHUB_TOKEN"
    }

    try {
        $invokeParams = @{
            Uri = $apiUrl
            Headers = $headers
        }
        if ((Get-Command Invoke-RestMethod).Parameters.ContainsKey("UseBasicParsing")) {
            $invokeParams.UseBasicParsing = $true
        }
        $release = Invoke-RestMethod @invokeParams
    }
    catch {
        throw "Could not retrieve GitHub release metadata from ${apiUrl}: $($_.Exception.Message)"
    }

    $asset = $release.assets | Where-Object { $_.name -eq $assetName } | Select-Object -First 1
    if (-not $asset) {
        throw "GitHub release asset metadata was not found for ${assetName} at ${apiUrl}."
    }

    $digest = [string]$asset.digest
    $match = [regex]::Match($digest, "(?i)^sha256:([a-f0-9]{64})$")
    if (-not $match.Success) {
        throw "GitHub release asset ${assetName} did not include a sha256 digest."
    }

    return $match.Groups[1].Value.ToLowerInvariant()
}

function Resolve-BinaryPackageSha256 {
    param(
        [string]$ToolName,
        [string]$BinaryUrl,
        [string]$ExplicitSha256,
        [string]$Sha256ParameterName
    )

    if ($ExplicitSha256) {
        return ($ExplicitSha256.Trim() -split "\s+")[0]
    }

    if (-not $BinaryUrl) {
        return ""
    }

    try {
        $githubSha256 = Resolve-GitHubReleaseAssetSha256 -BinaryUrl $BinaryUrl
    }
    catch {
        if ($_.Exception.Message -notlike "Could not retrieve GitHub release metadata from *") {
            throw
        }
        Write-Warning "Could not resolve GitHub release asset checksum for ${BinaryUrl}: $($_.Exception.Message). Continuing without SHA-256 verification."
        return ""
    }

    if ($githubSha256) {
        return $githubSha256
    }

    $parameterHint = if ($Sha256ParameterName) { "pass -$Sha256ParameterName" } else { "pass the matching SHA-256 parameter" }
    throw "No $ToolName binary checksum was available. For non-GitHub release URLs or local ZIP files, $parameterHint."
}

function Install-ZippedBinaryPackage {
    param(
        [string]$ToolName,
        [string]$BinaryUrl,
        [string]$DestinationPath,
        [string]$ExecutableName,
        [string]$ExpectedSha256
    )

    if (-not $BinaryUrl) {
        throw "No $ToolName binary URL was provided."
    }

    $tempZip = Join-Path $env:TEMP ("wgsextract-$ToolName-{0}.zip" -f ([guid]::NewGuid()))
    $extractDir = Join-Path $env:TEMP ("wgsextract-$ToolName-{0}" -f ([guid]::NewGuid()))
    try {
        Write-Host "Downloading prebuilt $ToolName from $BinaryUrl"
        Copy-UrlOrFile -Source $BinaryUrl -Destination $tempZip

        if ($ExpectedSha256) {
            $actualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $tempZip).Hash.ToLowerInvariant()
            if ($actualSha256 -ne $ExpectedSha256.ToLowerInvariant()) {
                throw "$ToolName binary checksum mismatch. Expected $ExpectedSha256 but got $actualSha256."
            }
            Write-Host "Verified $ToolName binary SHA256: $actualSha256"
        }

        New-Item -ItemType Directory -Path $extractDir -Force | Out-Null
        Expand-Archive -Path $tempZip -DestinationPath $extractDir -Force
        $binary = Get-ChildItem -Path $extractDir -Recurse -Filter $ExecutableName | Select-Object -First 1
        if (-not $binary) {
            throw "Downloaded $ToolName package did not contain $ExecutableName."
        }

        Copy-Item -LiteralPath $binary.FullName -Destination $DestinationPath -Force
        Write-Host "Installed prebuilt $ToolName to $DestinationPath"
    }
    finally {
        Remove-Item $tempZip -Force -ErrorAction SilentlyContinue
        Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Install-BwaBinaryPackage {
    param(
        [string]$BinaryUrl,
        [string]$DestinationPath
    )

    $expectedSha256 = Resolve-BinaryPackageSha256 -ToolName "BWA" -BinaryUrl $BinaryUrl -ExplicitSha256 $BwaBinarySha256 -Sha256ParameterName "BwaBinarySha256"
    Install-ZippedBinaryPackage -ToolName "BWA" -BinaryUrl $BinaryUrl -DestinationPath $DestinationPath -ExecutableName "bwa.exe" -ExpectedSha256 $expectedSha256
}

function Install-Minimap2BinaryPackage {
    param(
        [string]$BinaryUrl,
        [string]$DestinationPath
    )

    $expectedSha256 = Resolve-BinaryPackageSha256 -ToolName "minimap2" -BinaryUrl $BinaryUrl -ExplicitSha256 $Minimap2BinarySha256 -Sha256ParameterName "Minimap2BinarySha256"
    Install-ZippedBinaryPackage -ToolName "minimap2" -BinaryUrl $BinaryUrl -DestinationPath $DestinationPath -ExecutableName "minimap2.exe" -ExpectedSha256 $expectedSha256
}

function Install-SamblasterBinaryPackage {
    param(
        [string]$BinaryUrl,
        [string]$DestinationPath
    )

    $expectedSha256 = Resolve-BinaryPackageSha256 -ToolName "samblaster" -BinaryUrl $BinaryUrl -ExplicitSha256 $SamblasterBinarySha256 -Sha256ParameterName "SamblasterBinarySha256"
    Install-ZippedBinaryPackage -ToolName "samblaster" -BinaryUrl $BinaryUrl -DestinationPath $DestinationPath -ExecutableName "samblaster.exe" -ExpectedSha256 $expectedSha256
}

function Install-FastpBinaryPackage {
    param(
        [string]$BinaryUrl,
        [string]$DestinationPath
    )

    $expectedSha256 = Resolve-BinaryPackageSha256 -ToolName "fastp" -BinaryUrl $BinaryUrl -ExplicitSha256 $FastpBinarySha256 -Sha256ParameterName "FastpBinarySha256"
    Install-ZippedBinaryPackage -ToolName "fastp" -BinaryUrl $BinaryUrl -DestinationPath $DestinationPath -ExecutableName "fastp.exe" -ExpectedSha256 $expectedSha256
}

Assert-SafeReleaseValue -Name "BwaVersion" -Value $BwaVersion
Assert-SafeReleaseValue -Name "Minimap2Version" -Value $Minimap2Version
Assert-SafeReleaseValue -Name "SamblasterVersion" -Value $SamblasterVersion
Assert-SafeReleaseValue -Name "FastpVersion" -Value $FastpVersion
Assert-SafeReleaseValue -Name "ReleaseTag" -Value $ReleaseTag -AllowEmpty

$Msys2Root = [System.IO.Path]::GetFullPath($Msys2Root)
if (-not $BwaBinaryUrl) {
    if ($ReleaseTag -eq "latest") {
        $BwaBinaryUrl = "https://github.com/theontho/wgsextract-cli/releases/latest/download/wgsextract-bwa-$BwaVersion-windows-ucrt64.zip"
    } else {
        $BwaBinaryUrl = "https://github.com/theontho/wgsextract-cli/releases/download/$ReleaseTag/wgsextract-bwa-$BwaVersion-windows-ucrt64.zip"
    }
}
if (-not $Minimap2BinaryUrl) {
    if ($ReleaseTag -eq "latest") {
        $Minimap2BinaryUrl = "https://github.com/theontho/wgsextract-cli/releases/latest/download/wgsextract-minimap2-$Minimap2Version-windows-ucrt64.zip"
    } else {
        $Minimap2BinaryUrl = "https://github.com/theontho/wgsextract-cli/releases/download/$ReleaseTag/wgsextract-minimap2-$Minimap2Version-windows-ucrt64.zip"
    }
}
if (-not $SamblasterBinaryUrl) {
    if ($ReleaseTag -eq "latest") {
        $SamblasterBinaryUrl = "https://github.com/theontho/wgsextract-cli/releases/latest/download/wgsextract-samblaster-$SamblasterVersion-windows-ucrt64.zip"
    } else {
        $SamblasterBinaryUrl = "https://github.com/theontho/wgsextract-cli/releases/download/$ReleaseTag/wgsextract-samblaster-$SamblasterVersion-windows-ucrt64.zip"
    }
}
if (-not $FastpBinaryUrl) {
    if ($ReleaseTag -eq "latest") {
        $FastpBinaryUrl = "https://github.com/theontho/wgsextract-cli/releases/latest/download/wgsextract-fastp-$FastpVersion-windows-ucrt64.zip"
    } else {
        $FastpBinaryUrl = "https://github.com/theontho/wgsextract-cli/releases/download/$ReleaseTag/wgsextract-fastp-$FastpVersion-windows-ucrt64.zip"
    }
}
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
    $mandatoryRuntimePackages = @(
        "gzip",
        "tar",
        "mingw-w64-ucrt-x86_64-bcftools",
        "mingw-w64-ucrt-x86_64-htslib",
        "mingw-w64-ucrt-x86_64-samtools",
        "mingw-w64-ucrt-x86_64-zlib"
    )
    $optionalRuntimePackages = @(
        # Optional tools currently available from MSYS2 UCRT64.
        # htsfile is provided by mingw-w64-ucrt-x86_64-htslib above.
        "mingw-w64-ucrt-x86_64-curl",
        # Runtime DLLs needed by optional native release assets.
        "mingw-w64-ucrt-x86_64-gcc-libs",
        "mingw-w64-ucrt-x86_64-isa-l",
        "mingw-w64-ucrt-x86_64-libdeflate"
    )
    $runtimePackages = $mandatoryRuntimePackages + $optionalRuntimePackages
    Install-PacmanPackages "Installing MSYS2 UCRT64 runtime packages..." $runtimePackages
}
else {
    Write-Host "Skipping MSYS2 package installation."
}

$bwaPath = Join-Path $ucrt64Bin "bwa.exe"
$installedPrebuiltBwa = $false

if ((-not $SkipBwaBuild) -and (-not $ForceBwaBuild) -and (-not $SkipBwaDownload) -and (-not (Test-Path $bwaPath))) {
    try {
        Install-BwaBinaryPackage -BinaryUrl $BwaBinaryUrl -DestinationPath $bwaPath
        $installedPrebuiltBwa = $true
    }
    catch {
        Write-Warning "Prebuilt BWA install failed: $($_.Exception.Message)"
        Write-Warning "Falling back to a local MSYS2 UCRT64 BWA build."
    }
}

$shouldBuildBwa = (-not $SkipBwaBuild) -and ($ForceBwaBuild -or -not (Test-Path $bwaPath))

if ($shouldBuildBwa) {
    if (-not $SkipPackageInstall) {
        $buildPackages = @(
            "base-devel",
            "curl",
            "git",
            "make",
            "mingw-w64-ucrt-x86_64-gcc"
        )
        Install-PacmanPackages "Installing MSYS2 UCRT64 BWA build packages..." $buildPackages
    }

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
if [ -t 2 ]; then
    curl_progress='--progress-bar'
else
    curl_progress='--silent --show-error'
fi
curl -L `$curl_progress --retry 3 -o '$archiveName' '$sourceUrl'
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
elseif ($installedPrebuiltBwa) {
    Write-Host "BWA installed from prebuilt release package."
}
else {
    Write-Host "BWA already exists at $bwaPath. Use -ForceBwaBuild to rebuild locally."
}

$minimap2Path = Join-Path $ucrt64Bin "minimap2.exe"
$installedPrebuiltMinimap2 = $false

if ((-not $SkipMinimap2Build) -and (-not $ForceMinimap2Build) -and (-not $SkipMinimap2Download) -and (-not (Test-Path $minimap2Path))) {
    try {
        Install-Minimap2BinaryPackage -BinaryUrl $Minimap2BinaryUrl -DestinationPath $minimap2Path
        $installedPrebuiltMinimap2 = $true
    }
    catch {
        Write-Warning "Prebuilt minimap2 install failed: $($_.Exception.Message)"
        Write-Warning "Falling back to a local MSYS2 UCRT64 minimap2 build."
    }
}

$shouldBuildMinimap2 = (-not $SkipMinimap2Build) -and ($ForceMinimap2Build -or -not (Test-Path $minimap2Path))

if ($shouldBuildMinimap2) {
    try {
        if (-not $SkipPackageInstall) {
            $minimap2BuildPackages = @(
                "make",
                "mingw-w64-ucrt-x86_64-gcc",
                "mingw-w64-ucrt-x86_64-zlib"
            )
            Install-PacmanPackages "Installing MSYS2 UCRT64 minimap2 build packages..." $minimap2BuildPackages
        }

        $buildRoot = Resolve-RepoRelativePath $BuildDir
        New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
        $buildRootMsys = ConvertTo-MsysPath $buildRoot
        $archiveName = "minimap2-$Minimap2Version.tar.gz"
        $sourceDir = "minimap2-$Minimap2Version"
        $sourceUrl = "https://github.com/lh3/minimap2/archive/refs/tags/v$Minimap2Version.tar.gz"

        Write-Host "Building minimap2 $Minimap2Version for MSYS2 UCRT64..."
        Invoke-Msys2Script @"
mkdir -p '$buildRootMsys'
cd '$buildRootMsys'
rm -rf '$sourceDir' '$archiveName'
if [ -t 2 ]; then
    curl_progress='--progress-bar'
else
    curl_progress='--silent --show-error'
fi
curl -L `$curl_progress --retry 3 -o '$archiveName' '$sourceUrl'
tar -xzf '$archiveName'
cd '$sourceDir'
make CC=gcc
if [ -f minimap2.exe ]; then
    built_minimap2=minimap2.exe
else
    built_minimap2=minimap2
fi
install -m 755 "`$built_minimap2" /ucrt64/bin/minimap2.exe
/ucrt64/bin/minimap2.exe --version
"@
    }
    catch {
        if ($ForceMinimap2Build) {
            throw "Failed to build required minimap2 native runtime: $($_.Exception.Message)"
        }
        Write-Warning "Could not install optional minimap2 native runtime: $($_.Exception.Message)"
    }
}
elseif ($SkipMinimap2Build) {
    Write-Host "Skipping minimap2 build."
}
elseif ($installedPrebuiltMinimap2) {
    Write-Host "minimap2 installed from prebuilt release package."
}
else {
    Write-Host "minimap2 already exists at $minimap2Path. Use -ForceMinimap2Build to rebuild locally."
}

$samblasterPath = Join-Path $ucrt64Bin "samblaster.exe"
$installedPrebuiltSamblaster = $false

if ((-not $SkipSamblasterBuild) -and (-not $ForceSamblasterBuild) -and (-not $SkipSamblasterDownload) -and (-not (Test-Path $samblasterPath))) {
    try {
        Install-SamblasterBinaryPackage -BinaryUrl $SamblasterBinaryUrl -DestinationPath $samblasterPath
        $installedPrebuiltSamblaster = $true
    }
    catch {
        Write-Warning "Prebuilt samblaster install failed: $($_.Exception.Message)"
        Write-Warning "Falling back to a local MSYS2 UCRT64 samblaster build."
    }
}

$shouldBuildSamblaster = (-not $SkipSamblasterBuild) -and ($ForceSamblasterBuild -or -not (Test-Path $samblasterPath))

if ($shouldBuildSamblaster) {
    try {
        if (-not $SkipPackageInstall) {
            $samblasterBuildPackages = @(
                "make",
                "mingw-w64-ucrt-x86_64-gcc"
            )
            Install-PacmanPackages "Installing MSYS2 UCRT64 samblaster build packages..." $samblasterBuildPackages
        }

        $buildRoot = Resolve-RepoRelativePath $BuildDir
        New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
        $buildRootMsys = ConvertTo-MsysPath $buildRoot
        $archiveName = "samblaster-$SamblasterVersion.tar.gz"
        $sourceDir = "samblaster-v.$SamblasterVersion"
        $sourceUrl = "https://github.com/GregoryFaust/samblaster/archive/refs/tags/v.$SamblasterVersion.tar.gz"
        if ($SamblasterVersion -notmatch '\.(\d+)$') {
            throw "SamblasterVersion must end with a numeric build component."
        }
        $samblasterBuildNumber = $Matches[1]

        Write-Host "Building samblaster $SamblasterVersion for MSYS2 UCRT64..."
        Invoke-Msys2Script @"
mkdir -p '$buildRootMsys'
cd '$buildRootMsys'
rm -rf '$sourceDir' '$archiveName'
if [ -t 2 ]; then
    curl_progress='--progress-bar'
else
    curl_progress='--silent --show-error'
fi
curl -L `$curl_progress --retry 3 -o '$archiveName' '$sourceUrl'
tar -xzf '$archiveName'
cd '$sourceDir'
mkdir -p sys
cat > sys/times.h <<'EOF'
#ifndef WGSEXTRACT_SAMBLASTER_TIMES_H
#define WGSEXTRACT_SAMBLASTER_TIMES_H
#include <time.h>
struct tms { clock_t tms_utime; clock_t tms_stime; clock_t tms_cutime; clock_t tms_cstime; };
static inline clock_t times(struct tms *buffer) { (void)buffer; return 0; }
#endif
EOF
cat > sys/resource.h <<'EOF'
#ifndef WGSEXTRACT_SAMBLASTER_RESOURCE_H
#define WGSEXTRACT_SAMBLASTER_RESOURCE_H
#include <string.h>
#include <sys/time.h>
#ifndef RUSAGE_SELF
#define RUSAGE_SELF 0
#endif
struct rusage { struct timeval ru_utime; struct timeval ru_stime; long ru_maxrss; };
static inline int getrusage(int who, struct rusage *usage) { (void)who; memset(usage, 0, sizeof(*usage)); return 0; }
#endif
EOF
cat > sys/mman.h <<'EOF'
#ifndef WGSEXTRACT_SAMBLASTER_MMAN_H
#define WGSEXTRACT_SAMBLASTER_MMAN_H
#include <stddef.h>
#include <stdlib.h>
#define PROT_READ 1
#define PROT_WRITE 2
#define MAP_PRIVATE 2
#define MAP_SHARED 1
#define MAP_ANON 0x20
#define MAP_ANONYMOUS MAP_ANON
#define MAP_FAILED ((void *)-1)
static inline void *mmap(void *addr, size_t length, int prot, int flags, int fd, long offset) { (void)addr; (void)prot; (void)flags; (void)fd; (void)offset; void *ptr = malloc(length); return ptr ? ptr : MAP_FAILED; }
static inline int munmap(void *addr, size_t length) { (void)length; free(addr); return 0; }
#endif
EOF
cat > win_compat.h <<'EOF'
#ifndef WGSEXTRACT_SAMBLASTER_WIN_COMPAT_H
#define WGSEXTRACT_SAMBLASTER_WIN_COMPAT_H
#ifdef _WIN32
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#ifndef ssize_t
typedef long long ssize_t;
#endif
static inline int wgsextract_vasprintf(char **strp, const char *fmt, va_list ap) {
    va_list copy;
    va_copy(copy, ap);
    int length = vsnprintf(NULL, 0, fmt, copy);
    va_end(copy);
    if (length < 0) { *strp = NULL; return -1; }
    *strp = (char *)malloc((size_t)length + 1);
    if (!*strp) { return -1; }
    int written = vsnprintf(*strp, (size_t)length + 1, fmt, ap);
    if (written < 0) { free(*strp); *strp = NULL; return -1; }
    return written;
}
static inline int wgsextract_asprintf(char **strp, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int result = wgsextract_vasprintf(strp, fmt, ap);
    va_end(ap);
    return result;
}
static inline ssize_t wgsextract_getline(char **lineptr, size_t *n, FILE *stream) {
    if (!lineptr || !n || !stream) { return -1; }
    if (!*lineptr || *n == 0) {
        *n = 256;
        *lineptr = (char *)malloc(*n);
        if (!*lineptr) { return -1; }
    }
    size_t length = 0;
    int ch;
    while ((ch = fgetc(stream)) != EOF) {
        if (length + 1 >= *n) {
            size_t newSize = (*n) * 2;
            char *newLine = (char *)realloc(*lineptr, newSize);
            if (!newLine) { return -1; }
            *lineptr = newLine;
            *n = newSize;
        }
        (*lineptr)[length++] = (char)ch;
        if (ch == '\n') { break; }
    }
    if (length == 0 && ch == EOF) { return -1; }
    (*lineptr)[length] = '\0';
    return (ssize_t)length;
}
#define asprintf wgsextract_asprintf
#define getline wgsextract_getline
#endif
#endif
EOF
make CPP=g++ CPPFLAGS='-I. -include win_compat.h -Wall -O3 -D BUILDNUM=$samblasterBuildNumber'
if [ -f samblaster.exe ]; then
    built_samblaster=samblaster.exe
else
    built_samblaster=samblaster
fi
install -m 755 "`$built_samblaster" /ucrt64/bin/samblaster.exe
/ucrt64/bin/samblaster.exe --version
"@
    }
    catch {
        if ($ForceSamblasterBuild) {
            throw "Failed to build required samblaster native runtime: $($_.Exception.Message)"
        }
        Write-Warning "Could not install optional samblaster native runtime: $($_.Exception.Message)"
    }
}
elseif ($SkipSamblasterBuild) {
    Write-Host "Skipping samblaster build."
}
elseif ($installedPrebuiltSamblaster) {
    Write-Host "samblaster installed from prebuilt release package."
}
else {
    Write-Host "samblaster already exists at $samblasterPath. Use -ForceSamblasterBuild to rebuild locally."
}

$fastpPath = Join-Path $ucrt64Bin "fastp.exe"
$installedPrebuiltFastp = $false

if ((-not $SkipFastpBuild) -and (-not $ForceFastpBuild) -and (-not $SkipFastpDownload) -and (-not (Test-Path $fastpPath))) {
    try {
        Install-FastpBinaryPackage -BinaryUrl $FastpBinaryUrl -DestinationPath $fastpPath
        $installedPrebuiltFastp = $true
    }
    catch {
        Write-Warning "Prebuilt fastp install failed: $($_.Exception.Message)"
        Write-Warning "Falling back to a local MSYS2 UCRT64 fastp build."
    }
}

$shouldBuildFastp = (-not $SkipFastpBuild) -and ($ForceFastpBuild -or -not (Test-Path $fastpPath))

if ($shouldBuildFastp) {
    try {
        if (-not $SkipPackageInstall) {
            $fastpBuildPackages = @(
                "make",
                "mingw-w64-ucrt-x86_64-gcc",
                "mingw-w64-ucrt-x86_64-isa-l",
                "mingw-w64-ucrt-x86_64-libdeflate",
                "mingw-w64-ucrt-x86_64-zlib"
            )
            Install-PacmanPackages "Installing MSYS2 UCRT64 fastp build packages..." $fastpBuildPackages
        }

        $buildRoot = Resolve-RepoRelativePath $BuildDir
        New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
        $buildRootMsys = ConvertTo-MsysPath $buildRoot
        $archiveName = "fastp-$FastpVersion.tar.gz"
        $sourceDir = "fastp-$FastpVersion"
        $sourceUrl = "https://github.com/OpenGene/fastp/archive/refs/tags/v$FastpVersion.tar.gz"

        Write-Host "Building fastp $FastpVersion for MSYS2 UCRT64..."
        Invoke-Msys2Script @"
mkdir -p '$buildRootMsys'
cd '$buildRootMsys'
rm -rf '$sourceDir' '$archiveName'
if [ -t 2 ]; then
    curl_progress='--progress-bar'
else
    curl_progress='--silent --show-error'
fi
curl -L `$curl_progress --retry 3 -o '$archiveName' '$sourceUrl'
tar -xzf '$archiveName'
cd '$sourceDir'
sed -i \
    -e 's/struct stat status;/struct _stat64 status;/g' \
    -e 's/stat( s\.c_str(), \&status );/_stat64( s.c_str(), \&status );/g' \
    -e 's/stat( path\.c_str(), \&status );/_stat64( path.c_str(), \&status );/g' \
    src/util.h
make CXX=g++
if [ -f fastp.exe ]; then
    built_fastp=fastp.exe
else
    built_fastp=fastp
fi
install -m 755 "`$built_fastp" /ucrt64/bin/fastp.exe
/ucrt64/bin/fastp.exe --version
"@
    }
    catch {
        if ($ForceFastpBuild) {
            throw "Failed to build required fastp native runtime: $($_.Exception.Message)"
        }
        Write-Warning "Could not install optional fastp native runtime: $($_.Exception.Message)"
    }
}
elseif ($SkipFastpBuild) {
    Write-Host "Skipping fastp build."
}
elseif ($installedPrebuiltFastp) {
    Write-Host "fastp installed from prebuilt release package."
}
else {
    Write-Host "fastp already exists at $fastpPath. Use -ForceFastpBuild to rebuild locally."
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

$optionalPacmanTools = @("curl", "htsfile", "minimap2", "samblaster", "fastp")
$availableOptionalPacmanTools = @()
foreach ($tool in $optionalPacmanTools) {
    $toolPath = Join-Path $ucrt64Bin "$tool.exe"
    if (Test-Path $toolPath) {
        $availableOptionalPacmanTools += $tool
    }
}
if ($availableOptionalPacmanTools.Count -gt 0) {
    Write-Host "Optional pacman runtime tools are present: $($availableOptionalPacmanTools -join ', ')"
}

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
