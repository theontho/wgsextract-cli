# First-logon setup for the install-lab Windows base image (run by autounattend.xml).
#
# Gives the host a way in (OpenSSH Server with the lab key) and creates the
# standard "Lab User" user the scenario runs as. It installs nothing WGS Extract
# needs: the base image must look like a new user's PC.
param([Parameter(Mandatory = $true)][string]$Media)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Write-Host "lab-setup: media at $Media"

# Network driver for the VM's NIC, when the media carries one (virtio NetKVM).
$drivers = Join-Path $Media "drivers"
if (Test-Path -LiteralPath $drivers) {
    pnputil.exe /add-driver (Join-Path $drivers "*.inf") /subdirs /install | Out-Host
}

$deadline = (Get-Date).AddMinutes(15)
while ($true) {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "http://www.msftconnecttest.com/connecttest.txt" -TimeoutSec 10 | Out-Null
        break
    }
    catch {
        if ((Get-Date) -gt $deadline) { throw "No network after 15 minutes: $_" }
        Start-Sleep -Seconds 10
    }
}
Write-Host "lab-setup: network is up"

# Automatic updates would install and reboot in the middle of a run.
$au = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
New-Item -Path $au -Force | Out-Null
Set-ItemProperty -Path $au -Name NoAutoUpdate -Value 1 -Type DWord
powercfg.exe /change standby-timeout-ac 0
powercfg.exe /change monitor-timeout-ac 0
powercfg.exe /hibernate off

$capability = Get-WindowsCapability -Online -Name "OpenSSH.Server*" | Select-Object -First 1
if ($capability.State -ne "Installed") {
    Add-WindowsCapability -Online -Name $capability.Name | Out-Host
}
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
if (-not (Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}
# The capability's rule only covers Private networks; QEMU's NAT is Public (as
# an unknown network is on a user's PC), so open SSH there rather than
# reclassifying the network.
Set-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -Profile Any

function Set-KeyFile([string]$Path, [string]$Reader) {
    Copy-Item -LiteralPath (Join-Path $Media "lab_ed25519.pub") -Destination $Path -Force
    $grants = @("/grant", "Administrators:F", "/grant", "SYSTEM:F")
    if ($Reader) { $grants += @("/grant", "${Reader}:R") }
    icacls.exe $Path /inheritance:r @grants | Out-Null
}

Set-KeyFile "C:\ProgramData\ssh\administrators_authorized_keys" ""

# The scenario's user: a standard (non-admin) account, like a user running the
# installer without elevation. The space in its name puts one in every path
# under its profile (C:\Users\Lab User), as on many real PCs. Its key lives
# outside its profile, so the profile holds only what Windows and the
# installer put there.
$tester = "Lab User"
$password = ConvertTo-SecureString ([guid]::NewGuid().ToString() + "aA1!") -AsPlainText -Force
New-LocalUser -Name $tester -Password $password -PasswordNeverExpires -AccountNeverExpires | Out-Null
Add-LocalGroupMember -Group Users -Member $tester
# Windows' group for non-admin SSH users. A config with AllowGroups (GitHub's
# runner image has `AllowGroups administrators "openssh users"`) admits only
# its members; the stock config has none and ignores it.
if (Get-LocalGroup -Name "OpenSSH Users" -ErrorAction SilentlyContinue) {
    Add-LocalGroupMember -Group "OpenSSH Users" -Member $tester
}
Set-KeyFile "C:\ProgramData\ssh\labuser_authorized_keys" $tester
Add-Content -LiteralPath "C:\ProgramData\ssh\sshd_config" -Value @"

Match User "lab user"
       AuthorizedKeysFile __PROGRAMDATA__/ssh/labuser_authorized_keys
"@
Restart-Service sshd
Write-Host "lab-setup: done"
