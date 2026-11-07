# Where WGS Extract's MSYS2 is (dot-source this file): setup installs it there
# unless one already is, commands find their tools there, and uninstall removes
# it if setup installed it. WGSEXTRACT_MSYS2_ROOT overrides it.
#
# MSYS2 must live in a path of ASCII letters, digits and punctuation without
# spaces. With a space, its post-install scripts break: UCRT64's p11-kit runs
# "C:/Users/Lab User/.../trust.exe" unquoted and leaves an empty CA bundle, so
# every UCRT64 TLS client (curl, htslib fetching a CRAM's reference) rejects
# every server. %LOCALAPPDATA%\msys64 is used when its path qualifies;
# otherwise (a user name with a space or accent) a folder of the user's own,
# named by their SID, under %SystemDrive%\WGSExtract.
function Get-WGSExtractMsys2Root {
    if ($env:WGSEXTRACT_MSYS2_ROOT) { return $env:WGSEXTRACT_MSYS2_ROOT }
    $default = Join-Path $env:LOCALAPPDATA "msys64"
    if ($default -cmatch '^[A-Za-z0-9:\\._-]+$') { return $default }
    return Join-Path (Get-WGSExtractUserFolder) "msys64"
}

function Get-WGSExtractUserFolder {
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    return Join-Path $env:SystemDrive "WGSExtract\$sid"
}

# Creates the user's folder under %SystemDrive%\WGSExtract readable and writable
# by them alone (folders made in the drive's root are otherwise modifiable by
# every user). Returns whether it was created now.
function New-WGSExtractUserFolder {
    $folder = Get-WGSExtractUserFolder
    if (Test-Path -LiteralPath $folder) { return $false }
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $folder /inheritance:r /grant:r "*${sid}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not restrict access to $folder." }
    return $true
}
