# What the user does on their desktop, run in Lab User's signed-in session (by
# gui-scenario.ps1 through a scheduled task) because SSH sessions have no
# desktop: install the app with its wizard, start it, close its window,
# uninstall it from Settings > Apps.
#
# Usage: in-session.ps1 -Action install|launch|close|uninstall -Request <json>
# Writes <results>\session-<action>.log as it goes and, last, session-<action>.rc.
param(
    [Parameter(Mandatory = $true)][ValidateSet("install", "launch", "close", "uninstall")][string]$Action,
    [Parameter(Mandatory = $true)][string]$Request
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lab-common.ps1")
$req = Get-Content -LiteralPath $Request -Raw | ConvertFrom-Json
$log = Join-Path $req.results "session-$Action.log"
[System.IO.File]::WriteAllText($log, "", $Utf8)
# Each line is written at once, so a step that hangs shows where.
function Note([string]$Text) { [System.IO.File]::AppendAllText($log, "$(Get-Date -Format HH:mm:ss) $Text`r`n", $Utf8) }

# NSIS wizards, driven through UI Automation so the controls used are the
# wizard's real ones. NSIS's controls are plain Win32 buttons (UI Automation
# sees them only as panes): found by their text, and clicked by posting the
# BN_CLICKED notification a click sends the dialog. Posted, as a mouse click
# is: a click handler can run the next page's own message loop (MUI2's Finish
# page does), so a sent click wouldn't return until that page was left. Not
# BM_CLICK, which a dialog that isn't the foreground window ignores.
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$ae = [System.Windows.Automation.AutomationElement]
$scope = [System.Windows.Automation.TreeScope]
$anything = [System.Windows.Automation.Condition]::TrueCondition
Add-Type -Namespace Lab -Name Win32 -MemberDefinition @'
[DllImport("user32.dll")] public static extern System.IntPtr SendMessage(System.IntPtr hWnd, uint msg, System.IntPtr w, System.IntPtr l);
[DllImport("user32.dll")] public static extern bool PostMessage(System.IntPtr hWnd, uint msg, System.IntPtr w, System.IntPtr l);
[DllImport("user32.dll")] public static extern int GetDlgCtrlID(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern System.IntPtr GetParent(System.IntPtr hWnd);
'@

function Get-TopWindows { $ae::RootElement.FindAll($scope::Children, $anything) }

function Find-Window([string]$Pattern, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        foreach ($w in Get-TopWindows) { if ($w.Current.Name -like $Pattern) { return $w } }
        Start-Sleep -Milliseconds 500
    }
    throw "No window like '$Pattern' opened within $Seconds s."
}

function Find-Button($Window, [string]$Name, [int]$Seconds = 30) {
    $condition = New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition($ae::ClassNameProperty, "Button")),
        (New-Object System.Windows.Automation.PropertyCondition($ae::NameProperty, $Name)))
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ($true) {
        $found = $Window.FindFirst($scope::Descendants, $condition)
        if ($found -and $found.Current.IsEnabled -and -not $found.Current.IsOffscreen) { return $found }
        if ((Get-Date) -ge $deadline) { throw "No enabled button '$Name' in the wizard within $Seconds s." }
        Start-Sleep -Milliseconds 500
    }
}

function Send([object]$Control, [uint32]$Message) {
    [Lab.Win32]::SendMessage([IntPtr]$Control.Current.NativeWindowHandle, $Message, [IntPtr]::Zero, [IntPtr]::Zero).ToInt64()
}
function Click($Button) {
    $hwnd = [IntPtr]$Button.Current.NativeWindowHandle
    # WM_COMMAND with BN_CLICKED (0) in the high word and the control's ID.
    $wParam = [IntPtr][Lab.Win32]::GetDlgCtrlID($hwnd)
    if (-not [Lab.Win32]::PostMessage([Lab.Win32]::GetParent($hwnd), 0x0111, $wParam, $hwnd)) {
        throw "Could not click '$($Button.Current.Name)'."
    }
}
function Test-Checked($Box) { (Send $Box 0x00F0) -eq 1 }  # BM_GETCHECK

# What clicking an unticked checkbox does: it becomes ticked, and the dialog is told.
function Set-Ticked($Box) {
    if (Test-Checked $Box) { return }
    [Lab.Win32]::SendMessage([IntPtr]$Box.Current.NativeWindowHandle, 0x00F1, [IntPtr]1, [IntPtr]::Zero) | Out-Null  # BM_SETCHECK
    Click $Box
    if (-not (Test-Checked $Box)) { throw "'$($Box.Current.Name)' did not get ticked." }
}

# The text a page shows (headings, labels, checkboxes), for the evidence.
function Get-PageText($Window) {
    @($Window.FindAll($scope::Descendants, $anything) |
        Where-Object { $_.Current.Name -and -not $_.Current.IsOffscreen } |
        ForEach-Object { $_.Current.Name }) -join " | "
}

# Any other dialog from the wizard (an error, a warning) is read, answered with
# OK, and recorded; the caller fails the step if there was one.
function Read-MessageBoxes($Window, [string]$TitlePattern) {
    foreach ($w in Get-TopWindows) {
        if ($w.Current.ClassName -ne "#32770" -or $w.Current.NativeWindowHandle -eq $Window.Current.NativeWindowHandle -or
            $w.Current.Name -notlike $TitlePattern) { continue }
        $complaint = "message box '$($w.Current.Name)': $(Get-PageText $w)"
        Note $complaint
        Click (Find-Button $w "OK" 5)
        $complaint
    }
}

function Wait-WindowClosed($Window, [int]$Seconds) {
    $handle = $Window.Current.NativeWindowHandle
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-TopWindows | Where-Object { $_.Current.NativeWindowHandle -eq $handle })) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "The wizard is still open after $Seconds s."
}

# The installer as a user runs it: double-clicked, every page left at its
# defaults (Next, Install, Finish). Each page's text goes in the log. Whatever
# the Finish page's ticked boxes do (e.g. "Run WGSExtract") happens.
function Invoke-InstallWizard {
    Note "opening $($req.installer)"
    Start-Process -FilePath $req.installer
    $deadline = (Get-Date).AddSeconds(3600)
    $window = $null
    $handle = 0
    $lastPage = $null
    $complaints = @()
    while ($true) {
        if ((Get-Date) -ge $deadline) { throw "The install did not finish within an hour." }
        # Looked up on every pass: the installer may show another window with
        # the same title first (e.g. while it unpacks), then the wizard.
        $found = Find-Window $req.windowPattern 120
        if ($found.Current.NativeWindowHandle -ne $handle) {
            $window, $handle = $found, $found.Current.NativeWindowHandle
            Note "wizard window: '$($window.Current.Name)' ($handle)"
        }
        $complaints += @(Read-MessageBoxes $window "$($req.titlePrefix)*")
        $page = Get-PageText $window
        if ($page -ne $lastPage) { Note "page: $page"; $lastPage = $page }
        $button = $null
        foreach ($name in "Finish", "Install", "Next >") {
            try { $button = Find-Button $window $name 0; break } catch { }
        }
        if (-not $button) { Start-Sleep -Milliseconds 500; continue }
        $label = $button.Current.Name
        if ($label -eq "Finish") {
            $boxes = @($window.FindAll($scope::Descendants,
                    (New-Object System.Windows.Automation.PropertyCondition($ae::ClassNameProperty, "Button"))) |
                Where-Object { -not $_.Current.IsOffscreen -and $_.Current.Name -notin "Finish", "< Back", "Cancel" })
            foreach ($box in $boxes) { Note "finish page: '$($box.Current.Name)' ticked=$(Test-Checked $box)" }
        }
        Click $button
        Note "clicked $label"
        if ($label -eq "Finish") { break }
        # The next page replaces this one's controls; don't click it twice.
        $wait = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $wait -and (Get-PageText $window) -eq $page) { Start-Sleep -Milliseconds 200 }
    }
    Wait-WindowClosed $window 60
    Note "wizard closed"
    if ($complaints) { throw "The installer reported a problem: $($complaints -join '; ')" }
}

# The installer's own uninstall wizard, as Settings > Apps > Uninstall opens it:
# tick "Delete the application data", click Uninstall, then Close.
function Invoke-UninstallWizard {
    # Settings > Apps runs the registered UninstallString.
    $command = $req.uninstallString
    if ($command -notmatch '^\s*"([^"]+)"\s*(.*)$' -and $command -notmatch '^\s*(\S+)\s*(.*)$') {
        throw "Unparseable UninstallString: $command"
    }
    $exe, $arguments = $Matches[1], $Matches[2]
    Note "Settings > Apps > Uninstall runs: $command"
    if ($arguments) { Start-Process -FilePath $exe -ArgumentList $arguments } else { Start-Process -FilePath $exe }
    $window = Find-Window "$($req.displayName)*Uninstall*" 120
    Note "wizard: $($window.Current.Name)"
    Set-Ticked (Find-Button $window $req.deleteAppDataLabel)
    Note "ticked '$($req.deleteAppDataLabel)'"
    Click (Find-Button $window "Uninstall")
    Note "clicked Uninstall"
    # The bundle's uninstall (Pixi, MSYS2) runs inside the wizard; allow it time.
    $deadline = (Get-Date).AddSeconds(3600)
    $complaints = @()
    while ($true) {
        if ((Get-Date) -ge $deadline) { throw "The uninstall did not finish within an hour." }
        $complaints += @(Read-MessageBoxes $window "$($req.displayName)*")
        $close = $null
        try { $close = Find-Button $window "Close" 1 } catch { }
        if ($close) { break }
    }
    Click $close
    Note "clicked Close"
    if ($complaints) { throw "The uninstaller reported a problem: $($complaints -join '; ')" }
    Wait-WindowClosed $window 60
    Note "wizard closed"
}

$rc = 0
try {
    switch ($Action) {
        "install" { Invoke-InstallWizard }
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
Write-Text (Join-Path $req.results "session-$Action.rc") "$rc"
