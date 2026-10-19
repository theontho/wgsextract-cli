# Helpers shared by the Windows guest scripts (Windows PowerShell 5.1, as shipped).

$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Text([string]$Path, [string]$Text) {
    # UTF-8 without BOM: PowerShell 5.1's own redirection writes UTF-16.
    [System.IO.File]::WriteAllText($Path, $Text + "`n", $Utf8)
}

function ConvertTo-CmdArgument([string]$Value) {
    if ($Value -match '[\s"&|<>^()]' -or $Value -eq "") {
        return '"' + ($Value -replace '"', '""') + '"'
    }
    return $Value
}

# Run one step: <evidence>/<name>.{log,rc,seconds}. The command goes through
# cmd.exe so stdout and stderr land in one log as raw bytes, and only the
# process itself is awaited (MSYS2's gpg-agent outlives pacman-key).
function Invoke-Step {
    param(
        [string]$Evidence,
        [string]$Name,
        [string]$WorkingDirectory,
        [string[]]$Command
    )
    $log = Join-Path $Evidence "$Name.log"
    $line = ($Command | ForEach-Object { ConvertTo-CmdArgument $_ }) -join " "
    $cmdLine = '/d /s /c "' + $line + ' > ' + (ConvertTo-CmdArgument $log) + ' 2>&1 < NUL"'
    $start = Get-Date
    $process = Start-Process -FilePath "$env:SystemRoot\System32\cmd.exe" -ArgumentList $cmdLine `
        -WorkingDirectory $WorkingDirectory -NoNewWindow -PassThru
    $null = $process.Handle  # keeps ExitCode readable after exit
    $process.WaitForExit()
    $rc = $process.ExitCode
    Write-Text (Join-Path $Evidence "$Name.rc") "$rc"
    Write-Text (Join-Path $Evidence "$Name.seconds") ([int]((Get-Date) - $start).TotalSeconds)
    Write-Host "step $Name rc=$rc"
    return $rc
}

# What a new terminal sees: the registry's environment, not this session's.
function Update-SessionEnvironment {
    foreach ($scope in "Machine", "User") {
        $vars = [Environment]::GetEnvironmentVariables($scope)
        foreach ($key in $vars.Keys) {
            if ($key -ne "Path") { Set-Item -Path "Env:$key" -Value $vars[$key] }
        }
    }
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ";"
}
