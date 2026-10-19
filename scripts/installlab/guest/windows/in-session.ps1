# What the user does on their desktop, run in Lab User's signed-in session (by
# gui-scenario.ps1 through a scheduled task) because SSH sessions have no
# desktop: start the app, close its window, uninstall it from Settings > Apps.
#
# Usage: in-session.ps1 -Action launch|close|uninstall -Request <json>
# Writes <results>\session-<action>.log and, last, session-<action>.rc.
param(
    [Parameter(Mandatory = $true)][ValidateSet("launch", "close", "uninstall")][string]$Action,
    [Parameter(Mandatory = $true)][string]$Request
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lab-common.ps1")
$req = Get-Content -LiteralPath $Request -Raw | ConvertFrom-Json
$log = Join-Path $req.results "session-$Action.log"
$lines = New-Object System.Collections.Generic.List[string]
function Note([string]$Text) { $lines.Add("$(Get-Date -Format HH:mm:ss) $Text") }

# The installer's own uninstall wizard, as Settings > Apps > Uninstall opens it:
# tick "Delete the application data", click Uninstall, then Close. Driven
# through UI Automation, so the controls clicked are the wizard's real ones.
function Invoke-UninstallWizard {
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $ae = [System.Windows.Automation.AutomationElement]
    $scope = [System.Windows.Automation.TreeScope]
    function Find-Window([int]$Seconds) {
        $deadline = (Get-Date).AddSeconds($Seconds)
        while ((Get-Date) -lt $deadline) {
            foreach ($w in $ae::RootElement.FindAll($scope::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
                if ($w.Current.Name -like "$($req.displayName)*Uninstall*") { return $w }
            }
            Start-Sleep -Milliseconds 500
        }
        throw "The uninstall wizard did not open within $Seconds s."
    }
    # NSIS's controls are plain Win32 buttons (UI Automation sees them only as
    # panes): found by their text, clicked with the message a mouse click sends.
    Add-Type -Namespace Lab -Name Win32 -MemberDefinition @'
[DllImport("user32.dll")] public static extern System.IntPtr SendMessage(System.IntPtr hWnd, uint msg, System.IntPtr w, System.IntPtr l);
'@
    function Find-Button($Window, [string]$Name, [int]$Seconds = 30) {
        $condition = New-Object System.Windows.Automation.AndCondition(
            (New-Object System.Windows.Automation.PropertyCondition($ae::ClassNameProperty, "Button")),
            (New-Object System.Windows.Automation.PropertyCondition($ae::NameProperty, $Name)))
        $deadline = (Get-Date).AddSeconds($Seconds)
        while ((Get-Date) -lt $deadline) {
            $found = $Window.FindFirst($scope::Descendants, $condition)
            if ($found -and $found.Current.IsEnabled) { return $found }
            Start-Sleep -Milliseconds 500
        }
        throw "No enabled button '$Name' in the wizard within $Seconds s."
    }
    function Send([object]$Control, [uint32]$Message) {
        [Lab.Win32]::SendMessage([IntPtr]$Control.Current.NativeWindowHandle, $Message, [IntPtr]::Zero, [IntPtr]::Zero).ToInt64()
    }
    function Click($Button) { Send $Button 0x00F5 | Out-Null }  # BM_CLICK
    function Test-Checked($Box) { (Send $Box 0x00F0) -eq 1 }  # BM_GETCHECK

    # Settings > Apps runs the registered UninstallString.
    $command = $req.uninstallString
    if ($command -notmatch '^\s*"([^"]+)"\s*(.*)$' -and $command -notmatch '^\s*(\S+)\s*(.*)$') {
        throw "Unparseable UninstallString: $command"
    }
    $exe, $arguments = $Matches[1], $Matches[2]
    Note "Settings > Apps > Uninstall runs: $command"
    if ($arguments) { Start-Process -FilePath $exe -ArgumentList $arguments } else { Start-Process -FilePath $exe }
    $window = Find-Window 120
    Note "wizard: $($window.Current.Name)"
    $box = Find-Button $window $req.deleteAppDataLabel
    if (-not (Test-Checked $box)) { Click $box }
    if (-not (Test-Checked $box)) { throw "'$($req.deleteAppDataLabel)' did not get ticked." }
    Note "ticked '$($req.deleteAppDataLabel)'"
    Click (Find-Button $window "Uninstall")
    Note "clicked Uninstall"
    # The bundle's uninstall (Pixi, MSYS2) runs inside the wizard; allow it
    # time. A message box (e.g. a cleanup failure) is read, answered and fails the step.
    $deadline = (Get-Date).AddSeconds(3600)
    $complaint = $null
    while ($true) {
        if ((Get-Date) -ge $deadline) { throw "The uninstall did not finish within an hour." }
        foreach ($w in $ae::RootElement.FindAll($scope::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
            if ($w.Current.ClassName -ne "#32770" -or $w.Current.NativeWindowHandle -eq $window.Current.NativeWindowHandle -or
                $w.Current.Name -notlike "$($req.displayName)*") { continue }
            $texts = @($w.FindAll($scope::Descendants, [System.Windows.Automation.Condition]::TrueCondition) |
                ForEach-Object { $_.Current.Name } | Where-Object { $_ })
            $complaint = "message box '$($w.Current.Name)': $($texts -join ' | ')"
            Note $complaint
            Click (Find-Button $w "OK" 5)
        }
        $close = $null
        try { $close = Find-Button $window "Close" 1 } catch { }
        if ($close) { break }
    }
    Click $close
    Note "clicked Close"
    if ($complaint) { throw "The uninstaller reported a problem: $complaint" }
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        $open = @($ae::RootElement.FindAll($scope::Children, [System.Windows.Automation.Condition]::TrueCondition) |
            Where-Object { $_.Current.Name -like "$($req.displayName)*Uninstall*" })
        if ($open.Count -eq 0) { Note "wizard closed"; return }
        Start-Sleep -Milliseconds 500
    }
    throw "The uninstall wizard is still open."
}

$rc = 0
try {
    switch ($Action) {
        "launch" {
            foreach ($p in $req.env.PSObject.Properties) { Set-Item -LiteralPath "Env:$($p.Name)" -Value $p.Value }
            $launch = @{ FilePath = $req.exe; WorkingDirectory = $req.cwd; PassThru = $true
                RedirectStandardOutput = $req.stdout; RedirectStandardError = $req.stderr }
            if ($req.args) { $launch.ArgumentList = [string[]]$req.args }
            $app = Start-Process @launch
            Note "started $($req.exe) (pid $($app.Id))"
        }
        "close" {
            # Clicking the window's close button.
            $apps = @(Get-Process -Name $req.processName -ErrorAction SilentlyContinue)
            if ($apps.Count -eq 0) { throw "The app is not running." }
            foreach ($app in $apps) {
                Note "closing pid $($app.Id): $($app.CloseMainWindow())"
                if (-not $app.WaitForExit(60000)) { throw "pid $($app.Id) did not exit within 60 s." }
            }
        }
        "uninstall" {
            foreach ($p in $req.env.PSObject.Properties) { Set-Item -LiteralPath "Env:$($p.Name)" -Value $p.Value }
            Invoke-UninstallWizard
        }
    }
} catch {
    Note "ERROR: $($_ | Out-String)"
    $rc = 1
}
[System.IO.File]::WriteAllLines($log, [string[]]$lines, $Utf8)
Write-Text (Join-Path $req.results "session-$Action.rc") "$rc"
