# A minimal WebView2 app (a WinForms window with a WebView2 control), for the
# app VM's control runs: what Windows and the WebView2 runtime keep for any
# WebView2 app's window, so it isn't charged to the app under test.
#
# Usage: webview2-window.ps1 <folder with Microsoft.Web.WebView2.{Core,WinForms}.dll
#        and WebView2Loader.dll> <seconds>
param([string]$Sdk, [int]$Seconds = 20)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Path (Join-Path $Sdk "Microsoft.Web.WebView2.Core.dll")
Add-Type -Path (Join-Path $Sdk "Microsoft.Web.WebView2.WinForms.dll")
$env:PATH = "$Sdk;$env:PATH"  # WebView2Loader.dll

$form = New-Object System.Windows.Forms.Form
$form.Text = "WebView2 control window"
$form.Width = 800
$form.Height = 600
$view = New-Object Microsoft.Web.WebView2.WinForms.WebView2
$view.Dock = "Fill"
# Its own data folder, removed afterwards: only what lives outside it remains.
$data = Join-Path $env:TEMP ("webview2-control-" + [guid]::NewGuid().ToString("N"))
$view.CreationProperties = New-Object Microsoft.Web.WebView2.WinForms.CoreWebView2CreationProperties
$view.CreationProperties.UserDataFolder = $data
# A page like the app's: a terminal pane that keeps scrolling new output.
$page = "<body style='margin:0'><pre id=t style='height:100vh;overflow:auto;margin:0'></pre><script>" +
    "let n=0;setInterval(()=>{const t=document.getElementById('t');" +
    "t.textContent+='line '+(n++)+' downloading package '+Math.random()+'\n';t.scrollTop=t.scrollHeight},20)</script></body>"
$view.Source = [Uri]("data:text/html," + [Uri]::EscapeDataString($page))
$form.Controls.Add($view)
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $Seconds * 1000
$timer.Add_Tick({ $form.Close() })
$timer.Start()
[System.Windows.Forms.Application]::Run($form)
$view.Dispose()
Start-Sleep -Seconds 5
Remove-Item -LiteralPath $data -Recurse -Force -ErrorAction SilentlyContinue
