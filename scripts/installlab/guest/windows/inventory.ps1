# Snapshot Windows state an installer could touch, one "kind<TAB>path[<TAB>value]"
# per line, paths with forward slashes (the host's oracle and noise patterns are
# shared with macOS/Linux).
#
# Usage: inventory.ps1 <output-file> <exclude-dir>
param([string]$Out, [string]$Exclude)

$ErrorActionPreference = "SilentlyContinue"
. (Join-Path $PSScriptRoot "lab-common.ps1")
$lines = New-Object System.Collections.Generic.List[string]
$excludeSlash = if ($Exclude) { $Exclude.Replace("\", "/").TrimEnd("/") } else { "" }

function Add-Line([string]$Kind, [string]$Path, [string]$Value) {
    $p = $Path.Replace("\", "/")
    if ($excludeSlash -and ($p -ieq $excludeSlash -or $p.StartsWith("$excludeSlash/", "OrdinalIgnoreCase"))) { return }
    if ($PSBoundParameters.ContainsKey("Value")) { $lines.Add("$Kind`t$p`t$Value") } else { $lines.Add("$Kind`t$p") }
}

# A walk can fail part-way (a file vanishing mid-walk, a sharing violation) and
# silently skip a subtree, which would look like files appeared or vanished.
# Retry until a walk finishes with no error other than an access denial;
# otherwise emit an error line, which fails the run. The \\?\ prefix lets
# PowerShell 5.1 walk paths over 260 characters (Pixi environments have them).
function Add-Tree([string]$Root, [int]$Depth) {
    if (-not (Test-Path -LiteralPath $Root)) { return }
    $long = "\\?\" + $Root
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        $walkErrors = $null
        # -Depth counts levels below Root.
        $items = @(Get-ChildItem -LiteralPath $long -Force -Recurse -Depth ($Depth - 1) `
                -ErrorAction SilentlyContinue -ErrorVariable walkErrors)
        $real = @($walkErrors | Where-Object { $_.Exception -isnot [System.UnauthorizedAccessException] })
        if ($real.Count -eq 0) { break }
        Start-Sleep -Seconds 1
    }
    if ($real.Count -gt 0) {
        $lines.Add("error`tinventory walk of $($Root.Replace('\', '/')) failed: $($real[0].Exception.Message)")
    }
    Add-Line "dir" $Root
    foreach ($item in $items) {
        $kind = if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { "link" }
            elseif ($item.PSIsContainer) { "dir" } else { "file" }
        Add-Line $kind ($item.FullName -replace '^\\\\\?\\', '')
    }
}

# Raw (unexpanded) data and type: %USERPROFILE%\... turning into a literal
# path, or REG_EXPAND_SZ into REG_SZ, is a change an uninstall must undo.
function Add-RegistryValues([string]$Key) {
    $item = Get-Item -LiteralPath $Key
    if (-not $item) { return }
    foreach ($name in $item.GetValueNames()) {
        $raw = $item.GetValue($name, $null, "DoNotExpandEnvironmentNames")
        Add-Line "reg" "$Key\$name" ("{0}:{1}" -f $item.GetValueKind($name), ($raw -join "|"))
    }
}

function Add-RegistrySubkeys([string]$Key) {
    Get-ChildItem -LiteralPath $Key | ForEach-Object { Add-Line "regkey" ($_.Name) }
}

Add-Tree $env:USERPROFILE 6
Add-Tree "C:\" 1
Add-Tree $env:ProgramFiles 1
Add-Tree ${env:ProgramFiles(x86)} 1
Add-Tree $env:ProgramData 1
Add-Tree "$env:ProgramData\Microsoft\Windows\Start Menu\Programs" 2
# The user's Start menu is deeper than the profile walk reaches.
Add-Tree "$env:APPDATA\Microsoft\Windows\Start Menu\Programs" 2
# Crash reports (Windows Error Reporting) and user crash dumps.
Add-Tree "$env:ProgramData\Microsoft\Windows\WER" 3
Add-Tree "$env:LOCALAPPDATA\Microsoft\Windows\WER" 3
Add-Tree "$env:LOCALAPPDATA\CrashDumps" 1

Add-RegistryValues "HKCU:\Environment"
Add-RegistryValues "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
Add-RegistryValues "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Add-RegistryValues "HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce"
Add-RegistryValues "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run"
Add-RegistryValues "HKCU:\Software\Microsoft\Command Processor"
Add-RegistrySubkeys "HKCU:\Software"
Add-RegistrySubkeys "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall"
Add-RegistrySubkeys "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall"
Add-RegistrySubkeys "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
Get-ScheduledTask | ForEach-Object { Add-Line "task" ($_.TaskPath + $_.TaskName) }
# Programs left running (an installer's background agent locks its files and
# outlives the uninstall). Windows' own are skipped; others' paths aren't
# visible to this user anyway.
Get-Process | Where-Object { $_.Path -and -not $_.Path.StartsWith($env:SystemRoot, "OrdinalIgnoreCase") } |
    ForEach-Object { Add-Line "proc" $_.Path }

# Shell startup files: content matters, not just existence.
foreach ($profilePath in @($PROFILE.AllUsersAllHosts, $PROFILE.AllUsersCurrentHost,
        $PROFILE.CurrentUserAllHosts, $PROFILE.CurrentUserCurrentHost)) {
    if ($profilePath -and (Test-Path -LiteralPath $profilePath)) {
        Add-Line "rc" $profilePath (Get-FileHash -LiteralPath $profilePath -Algorithm SHA256).Hash
    }
}

$sorted = $lines | Sort-Object -Unique -CaseSensitive
[System.IO.File]::WriteAllLines($Out, [string[]]$sorted, $Utf8)
