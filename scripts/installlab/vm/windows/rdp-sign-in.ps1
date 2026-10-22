# Sign Lab User in over Remote Desktop on a GitHub Windows runner (lab.py run
# windows-host with the app; see WindowsHostGuiMachine in windows_gui_vm.py).
#
# Runs in the runner's own desktop session, where the Remote Desktop client
# (mstsc) asks for the account's credentials: fills in that dialog with UI
# Automation, as a person types them, and accepts the client's prompt about the
# server's certificate if one appears (the server is this machine).
# The password arrives in LAB_RDP_PASSWORD.

param([Parameter(Mandatory = $true)][string]$User, [int]$Timeout = 300)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
$UIA = [System.Windows.Automation.AutomationElement]
$Tree = [System.Windows.Automation.TreeScope]
$Prop = [System.Windows.Automation.AutomationElement]

function Find-All($Root, $Type) {
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        $Prop::ControlTypeProperty, [System.Windows.Automation.ControlType]::$Type)
    $Root.FindAll($Tree::Descendants, $cond)
}

function Find-Button($Root, $Names) {
    foreach ($b in (Find-All $Root "Button")) {
        if ($Names -contains $b.Current.Name) { return $b }
    }
    return $null
}

function Set-Text($Edit, $Text) {
    $Edit.SetFocus()
    $value = $null
    if ($Edit.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$value)) {
        try { $value.SetValue($Text); return } catch { }
    }
    # Password boxes may refuse SetValue: type it, with SendKeys' specials escaped.
    [System.Windows.Forms.SendKeys]::SendWait(($Text -replace '[+^%~(){}\[\]]', '{$0}'))
}

$deadline = (Get-Date).AddSeconds($Timeout)
$signedIn = $false
while ((Get-Date) -lt $deadline) {
    foreach ($w in $UIA::RootElement.FindAll($Tree::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
        $name = $w.Current.Name
        if ($name -eq "Windows Security" -and -not $signedIn) {
            $edits = @(Find-All $w "Edit")
            $ok = Find-Button $w @("OK")
            if ($edits.Count -lt 2 -or -not $ok) { continue }
            Write-Host "Filling in the credentials dialog"
            Set-Text $edits[0] $User
            Set-Text ($edits | Where-Object { $_.Current.IsPassword } | Select-Object -First 1) $env:LAB_RDP_PASSWORD
            $ok.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            $signedIn = $true
        } elseif ($name -like "*Remote Desktop Connection*") {
            # "The identity of the remote computer cannot be verified": it's this machine.
            $yes = Find-Button $w @("Yes")
            if ($yes) {
                Write-Host "Accepting the client's prompt: $name"
                $yes.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            }
        }
    }
    Start-Sleep -Seconds 2
}
