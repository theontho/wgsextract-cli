param(
    [Parameter(Mandatory=$true)][string]$Packages,
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$Destination
)
$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -lt 7) { throw "Use native ARM64 PowerShell 7" }
if ([System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture -ne "Arm64") {
    throw "An emulated PowerShell cannot install this candidate"
}
$pixi = (Get-Command pixi -ErrorAction Stop).Source
$stream = [IO.File]::OpenRead($pixi)
$reader = [IO.BinaryReader]::new($stream)
try {
    if ($stream.Length -lt 64 -or $reader.ReadUInt16() -ne 0x5A4D) {
        throw "Pixi has no valid DOS header"
    }
    $stream.Position = 60
    $offset = $reader.ReadUInt32()
    if ($offset -lt 64 -or $offset + 24 -gt $stream.Length) { throw "Invalid Pixi PE offset" }
    $stream.Position = $offset
    if ($reader.ReadUInt32() -ne 0x4550 -or $reader.ReadUInt16() -ne 0xAA64) {
        throw "Pixi is not native ARM64"
    }
} finally { $reader.Dispose() }
$Packages = (Resolve-Path $Packages).Path
$Source = (Resolve-Path $Source).Path
if (!(Test-Path "$Source\pyproject.toml")) { throw "Missing candidate CLI source" }
if (!(Test-Path "$Source\scripts\audit_windows_arm64.ps1")) { throw "Missing native audit" }
if (Test-Path $Destination) { throw "Fresh candidate destination must not exist" }
$manifest = Get-Content "$Packages\manifest.json" -Raw | ConvertFrom-Json
$expected = @("bcftools", "bwa", "bwa-mem2", "curl", "fastp", "fastqc", "haplogrep", "htslib", "minimap2", "openjdk", "python", "samblaster", "samtools", "setuptools")
if (@($manifest.packages).Count -ne $expected.Count) { throw "Incomplete native package set" }
foreach ($name in $expected) {
    if (@($manifest.packages | Where-Object name -eq $name).Count -ne 1) {
        throw "Missing or duplicate package: $name"
    }
}
$lines = @(
    "[workspace]",
    'name = "wgsextract-win-arm64-candidate"',
    'channels = ["conda-forge"]',
    'platforms = ["win-arm64"]',
    "",
    "[dependencies]"
)
foreach ($package in $manifest.packages) {
    if ($package.file -ne [IO.Path]::GetFileName($package.file)) { throw "Unsafe package path" }
    $path = Join-Path $Packages $package.file
    if ((Get-FileHash $path -Algorithm SHA256).Hash -ine $package.sha256) {
        throw "Candidate package checksum mismatch: $($package.file)"
    }
    if ($package.name -eq "python" -and $package.version -ne "3.13.18") {
        throw "Only the qualified secure Python 3.13.18 is permitted"
    }
    $url = ([uri]$path).AbsoluteUri | ConvertTo-Json -Compress
    $lines += "$($package.name) = { url = $url, sha256 = `"$($package.sha256)`" }"
}
$sourceString = ($Source -replace "\\", "/") | ConvertTo-Json -Compress
$lines += @(
    "",
    "[pypi-dependencies]",
    "wgsextract-cli = { path = $sourceString, editable = true }",
    'yleaf = { url = "https://github.com/theontho/Yleaf/archive/8890b3abc20616307a172a0185586d9e1fd4d545.tar.gz#sha256=3e3770ef3379ddbeedabb1cb0a11280a1485c4da12995802b52956f14e247379" }',
    'numpy = { url = "https://files.pythonhosted.org/packages/34/52/01d205e5e8ccb27b2b0b141e801f22b830198c979111b0fa44771438d9a9/numpy-2.5.4-cp313-cp313-win_arm64.whl#sha256=bc39ac66a7a9a3fbd6134fda43136b60ffde99c8f4501e64e0d2b24da137babf" }',
    'pandas = { url = "https://files.pythonhosted.org/packages/45/f7/8cdd350c67bc62865d1e30cd7add43dd631420223487ba42dfcfd0d0e052/pandas-3.1.0-cp313-cp313-win_arm64.whl#sha256=54fed4a50b8321ff8d413b46b7e31a023cecefd06c7210c02aa980a9836428b2" }',
    'psutil = { url = "https://files.pythonhosted.org/packages/8c/c7/7bb2e321574b10df20cbde462a94e2b71d05f9bbda251ef27d104668306a/psutil-7.2.2-cp37-abi3-win_arm64.whl#sha256=8c233660f575a5a89e6d4cb65d9f938126312bca76d8fe087b947b3a1aaac9ee" }',
    'six = "==1.17.0"'
)
New-Item -ItemType Directory -Path $Destination | Out-Null
$manifestPath = Join-Path $Destination "pixi.toml"
$lines | Set-Content $manifestPath -Encoding utf8NoBOM
& $pixi install --manifest-path $manifestPath
if ($LASTEXITCODE) { throw "Native candidate environment installation failed" }
& $pixi run --manifest-path $manifestPath python -c "import sys; assert sys.version_info[:3] == (3,13,18); from wgsextract_cli.core.windows_arm64 import assert_native_windows_python; assert_native_windows_python()"
if ($LASTEXITCODE) { throw "Installed interpreter is not qualified native Python" }
$prefix = (Resolve-Path (Join-Path $Destination ".pixi\envs\default")).Path
& "$Source\scripts\audit_windows_arm64.ps1" -Prefix $prefix `
    -Report (Join-Path $Destination "out\architecture.json")

# Schannel securely triggers Windows' normal automatic trusted-root distribution.
& "$prefix\Library\bin\curl.exe" --fail --silent --show-error --head --max-time 30 `
    https://hgdownload.soe.ucsc.edu/goldenPath/hg19/liftOver/hg19ToHg38.over.chain.gz
if ($LASTEXITCODE) { throw "Windows certificate trust/HTTPS preflight failed" }
& $pixi run --manifest-path $manifestPath python -c "from urllib.request import urlopen; r=urlopen('https://hgdownload.soe.ucsc.edu/goldenPath/hg19/liftOver/hg19ToHg38.over.chain.gz', timeout=30); assert r.status == 200; r.close()"
if ($LASTEXITCODE) { throw "Installed Python certificate verification failed" }
& $pixi run --manifest-path $manifestPath wgsextract deps check
if ($LASTEXITCODE) { throw "Native default tool dependency check failed" }
