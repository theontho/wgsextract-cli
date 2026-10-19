# Make a disposable GitHub Windows runner look like a new user's PC before the
# install lab's scenario runs on it (lab.py run windows-host; CI only).
#
# The runner image preinstalls MSYS2 at C:\msys64, Git, conda, Java, Python,
# Strawberry Perl and more, and puts them on the machine PATH. Any of them could
# make a broken installer pass, so: hide C:\msys64 (the installer's own default
# location), reset the machine PATH and drop non-default machine variables.
# The oracle's tool survey still fails the run if something leaks through.

$ErrorActionPreference = "Stop"

if (Test-Path -LiteralPath "C:\msys64") {
    Rename-Item -LiteralPath "C:\msys64" -NewName "msys64-hidden-by-install-lab"
}

$machineEnv = "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
$defaultPath = @(
    "%SystemRoot%\system32",
    "%SystemRoot%",
    "%SystemRoot%\System32\Wbem",
    "%SystemRoot%\System32\WindowsPowerShell\v1.0\",
    "%SystemRoot%\System32\OpenSSH\"
) -join ";"
Set-ItemProperty -LiteralPath $machineEnv -Name Path -Value $defaultPath -Type ExpandString

# Variables a stock Windows 11 install defines machine-wide.
$keep = @(
    "ComSpec", "DriverData", "NUMBER_OF_PROCESSORS", "OS", "Path", "PATHEXT",
    "POWERSHELL_DISTRIBUTION_CHANNEL", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL", "PROCESSOR_REVISION", "PSModulePath", "TEMP", "TMP",
    "USERNAME", "windir"
)
foreach ($name in (Get-Item -LiteralPath $machineEnv).Property) {
    if ($keep -notcontains $name) {
        Write-Host "Removing machine environment variable $name"
        Remove-ItemProperty -LiteralPath $machineEnv -Name $name
    }
}

# New logons (Lab User's SSH sessions) read the environment from the
# registry; restart sshd so its own environment matches too.
Restart-Service sshd
