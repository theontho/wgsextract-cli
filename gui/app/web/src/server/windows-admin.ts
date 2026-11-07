import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import type { ProcessRunOptions } from "../../../shared/types.js";
import { errorMessage } from "./errors.js";
import { powerShellSingleQuotedString } from "./platform-command.js";

const windowsAdminModeEnvironmentKey = "GUI_FOR_CLI_WINDOWS_ADMIN_MODE";
const windowsAdminScheduledTaskMode = "scheduled-task";
const windowsAdminTaskEnvironmentKey = "GUI_FOR_CLI_WINDOWS_ADMIN_TASK";
const windowsAdminQueueEnvironmentKey = "GUI_FOR_CLI_WINDOWS_ADMIN_QUEUE";

interface WindowsAdminSettings {
    mode: "uac" | "scheduled-task";
    taskName?: string;
    queueDirectory?: string;
}

export interface ElevatedCommand {
    command: { executable: string; args: string[] };
    cleanup: () => Promise<void>;
}

export async function windowsAdminCommand(
    command: { executable: string; args: string[] },
    options: ProcessRunOptions,
): Promise<ElevatedCommand> {
    const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-admin-"));
    try {
        const launcherPath = path.join(tempRoot, "launch.ps1");
        const stdoutPath = path.join(tempRoot, "stdout.txt");
        const stderrPath = path.join(tempRoot, "stderr.txt");
        const exitCodePath = path.join(tempRoot, "exit-code.txt");
        await writeFile(launcherPath, windowsAdminLauncherScript(command, options.elevatedEnv ?? {}, stdoutPath, stderrPath, exitCodePath), "utf8");
        const wrapper = windowsAdminWrapperScript(
            launcherPath,
            stdoutPath,
            stderrPath,
            exitCodePath,
            options.cwd,
            windowsAdminMode(options.env),
            options.timeoutMs,
        );
        return {
            command: {
                executable: "powershell.exe",
                args: ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", wrapper],
            },
            cleanup: () => rm(tempRoot, { force: true, recursive: true }),
        };
    }
    catch (error) {
        await cleanupElevated({ cleanup: () => rm(tempRoot, { force: true, recursive: true }) });
        throw error;
    }
}

export function windowsAdminLauncherScript(
    command: { executable: string; args: string[] },
    environment: Record<string, string | undefined>,
    stdoutPath: string,
    stderrPath: string,
    exitCodePath: string,
): string {
    return [
        "$ErrorActionPreference = 'Stop'",
        ...Object.entries(environment).map(([key, value]) => value == null
            ? `Remove-Item -LiteralPath ${powerShellSingleQuotedString(`Env:\\${key}`)} -ErrorAction SilentlyContinue`
            : `Set-Item -LiteralPath ${powerShellSingleQuotedString(`Env:\\${key}`)} -Value ${powerShellSingleQuotedString(value)}`),
        "$previousErrorActionPreference = $ErrorActionPreference",
        "try {",
        `  $process = Start-Process -FilePath ${powerShellSingleQuotedString(command.executable)} \``,
        `    -ArgumentList ${powerShellSingleQuotedString(windowsCommandLine(command.args))} \``,
        `    -RedirectStandardOutput ${powerShellSingleQuotedString(stdoutPath)} \``,
        `    -RedirectStandardError ${powerShellSingleQuotedString(stderrPath)} \``,
        "    -PassThru `",
        "    -NoNewWindow",
        // Without a cached handle, ExitCode reads as $null once the process has exited.
        "  $null = $process.Handle",
        "  $process.WaitForExit()",
        "  $exitCode = if ($process.ExitCode -is [int]) { $process.ExitCode } else { [Console]::Error.WriteLine('Admin command exit code was unavailable.'); 1 }",
        "} catch {",
        `  $_ | Out-File -FilePath ${powerShellSingleQuotedString(stderrPath)} -Append -Encoding utf8`,
        "  $exitCode = 1",
        "} finally {",
        "  $ErrorActionPreference = $previousErrorActionPreference",
        "}",
        `Set-Content -LiteralPath ${powerShellSingleQuotedString(exitCodePath)} -Value $exitCode -Encoding ascii`,
        "exit $exitCode",
        "",
    ].join("\n");
}

export function windowsAdminWrapperScript(
    launcherPath: string,
    stdoutPath: string,
    stderrPath: string,
    exitCodePath: string,
    workingDirectory: string | undefined,
    adminSettings: WindowsAdminSettings = { mode: "uac" },
    timeoutMs?: number,
): string {
    const timeoutSeconds = timeoutMs == null ? undefined : Math.max(1, Math.ceil(timeoutMs / 1000));
    return [
        "$ErrorActionPreference = 'Stop'",
        "$fileEncoding = [System.Text.UTF8Encoding]::new($false)",
        "[long]$stdoutPosition = 0",
        "[long]$stderrPosition = 0",
        ...(timeoutSeconds == null ? [] : [`$deadline = (Get-Date).AddSeconds(${timeoutSeconds})`]),
        "function Write-NewFileContent {",
        "  param([string]$Path, [ref]$Position, [bool]$IsError)",
        "  if (-not (Test-Path -LiteralPath $Path)) { return }",
        "  $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)",
        "  try {",
        "    if ($stream.Length -le $Position.Value) { return }",
        "    $stream.Position = $Position.Value",
        "    while ($stream.Position -lt $stream.Length) {",
        "      $count = [int][Math]::Min(65536, $stream.Length - $stream.Position)",
        "      $buffer = New-Object byte[] $count",
        "      $read = $stream.Read($buffer, 0, $count)",
        "      if ($read -le 0) { break }",
        "      $offset = if ($Position.Value -eq 0 -and $read -ge 3 -and $buffer[0] -eq 0xef -and $buffer[1] -eq 0xbb -and $buffer[2] -eq 0xbf) { 3 } else { 0 }",
        "      $Position.Value = $stream.Position",
        "      $text = $fileEncoding.GetString($buffer, $offset, $read - $offset)",
        "      if ($IsError) { [Console]::Error.Write($text) } else { [Console]::Out.Write($text) }",
        "    }",
        "  } finally {",
        "    $stream.Dispose()",
        "  }",
        "}",
        "try {",
        ...windowsAdminStartScript(adminSettings, launcherPath, stderrPath, exitCodePath, workingDirectory).map((line) => `  ${line}`),
        "  do {",
        "    Start-Sleep -Milliseconds 200",
        `    Write-NewFileContent -Path ${powerShellSingleQuotedString(stdoutPath)} -Position ([ref]$stdoutPosition) -IsError $false`,
        `    Write-NewFileContent -Path ${powerShellSingleQuotedString(stderrPath)} -Position ([ref]$stderrPosition) -IsError $true`,
        ...windowsAdminWaitScript(adminSettings, exitCodePath).map((line) => `    ${line}`),
        ...(timeoutSeconds == null ? [] : ["    if (-not $adminCommandCompleted -and (Get-Date) -ge $deadline) { throw 'Admin command did not complete before the process timeout.' }"]),
        "  } while (-not $adminCommandCompleted)",
        `  Write-NewFileContent -Path ${powerShellSingleQuotedString(stdoutPath)} -Position ([ref]$stdoutPosition) -IsError $false`,
        `  Write-NewFileContent -Path ${powerShellSingleQuotedString(stderrPath)} -Position ([ref]$stderrPosition) -IsError $true`,
        "} catch {",
        `  $_ | Out-File -FilePath ${powerShellSingleQuotedString(stderrPath)} -Append -Encoding utf8`,
        `  Write-NewFileContent -Path ${powerShellSingleQuotedString(stderrPath)} -Position ([ref]$stderrPosition) -IsError $true`,
        "  exit 1",
        "}",
        `$exitCode = if (Test-Path -LiteralPath ${powerShellSingleQuotedString(exitCodePath)}) { [int]([IO.File]::ReadAllText(${powerShellSingleQuotedString(exitCodePath)}, [System.Text.Encoding]::ASCII).Trim()) } elseif ($null -ne $process -and $process.ExitCode -is [int]) { $process.ExitCode } else { [Console]::Error.WriteLine('Admin command did not write an exit code.'); 1 }`,
        "exit $exitCode",
    ].join("\n");
}

function windowsAdminStartScript(
    adminSettings: WindowsAdminSettings,
    launcherPath: string,
    stderrPath: string,
    exitCodePath: string,
    workingDirectory: string | undefined,
) {
    if (adminSettings.mode === "scheduled-task") {
        if (!adminSettings.taskName || !adminSettings.queueDirectory) {
            throw new Error("Scheduled-task admin mode requires a task name and queue directory.");
        }
        return [
            `$requestDirectory = ${powerShellSingleQuotedString(adminSettings.queueDirectory)}`,
            "New-Item -ItemType Directory -Force -Path $requestDirectory | Out-Null",
            "$requestId = [guid]::NewGuid().ToString('N')",
            "$requestPath = Join-Path $requestDirectory ($requestId + '.pending.json')",
            "$request = [ordered]@{",
            `  launcherPath = ${powerShellSingleQuotedString(launcherPath)}`,
            `  stderrPath = ${powerShellSingleQuotedString(stderrPath)}`,
            `  exitCodePath = ${powerShellSingleQuotedString(exitCodePath)}`,
            ...(workingDirectory ? [`  workingDirectory = ${powerShellSingleQuotedString(workingDirectory)}`] : []),
            "}",
            `Set-Content -LiteralPath $requestPath -Value (($request | ConvertTo-Json -Depth 4) + "\`n") -Encoding utf8`,
            "$process = $null",
            `& schtasks.exe /Run /TN ${powerShellSingleQuotedString(adminSettings.taskName)} | Out-Null`,
            `if ($LASTEXITCODE -ne 0) { throw ${powerShellSingleQuotedString(`Could not trigger scheduled admin task '${adminSettings.taskName}'.`)} }`,
        ];
    }
    return [
        "$process = Start-Process -FilePath 'powershell.exe'",
        `  -ArgumentList ${powerShellSingleQuotedString(windowsCommandLine(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", launcherPath]))}`,
        "  -Verb RunAs",
        "  -PassThru",
        ...(workingDirectory ? [`  -WorkingDirectory ${powerShellSingleQuotedString(workingDirectory)}`] : []),
    ].join(" `\n").split("\n");
}

function windowsAdminWaitScript(adminSettings: WindowsAdminSettings, exitCodePath: string) {
    if (adminSettings.mode === "scheduled-task") {
        return [
            `$adminCommandCompleted = Test-Path -LiteralPath ${powerShellSingleQuotedString(exitCodePath)}`,
        ];
    }
    return [
        "$process.Refresh()",
        "$adminCommandCompleted = $process.HasExited",
        "if ($adminCommandCompleted) { $process.WaitForExit() }",
    ];
}

function windowsCommandLine(values: string[]) {
    return values.map(windowsCommandLineArgument).join(" ");
}

function windowsAdminMode(env: NodeJS.ProcessEnv | Record<string, string | undefined> | undefined) {
    if (env?.[windowsAdminModeEnvironmentKey] !== windowsAdminScheduledTaskMode) {
        return { mode: "uac" } satisfies WindowsAdminSettings;
    }
    const taskName = env[windowsAdminTaskEnvironmentKey];
    const queueDirectory = env[windowsAdminQueueEnvironmentKey];
    if (!taskName || !queueDirectory) {
        throw new Error(
            `${windowsAdminModeEnvironmentKey}=scheduled-task requires ${windowsAdminTaskEnvironmentKey} and ${windowsAdminQueueEnvironmentKey}.`,
        );
    }
    return { mode: "scheduled-task", taskName, queueDirectory } satisfies WindowsAdminSettings;
}

function windowsCommandLineArgument(value: string) {
    const text = String(value);
    if (!/[ \t\n\v"]/.test(text)) {
        return text;
    }
    let quoted = "\"";
    let backslashes = 0;
    for (const character of text) {
        if (character === "\\") {
            backslashes += 1;
        }
        else if (character === "\"") {
            quoted += "\\".repeat(backslashes * 2 + 1);
            quoted += character;
            backslashes = 0;
        }
        else {
            quoted += "\\".repeat(backslashes);
            quoted += character;
            backslashes = 0;
        }
    }
    return `${quoted}${"\\".repeat(backslashes * 2)}"`;
}


export async function cleanupElevated(elevated: { cleanup: () => Promise<void> } | undefined) {
    if (!elevated) {
        return;
    }
    try {
        await elevated.cleanup();
    }
    catch (error) {
        console.warn(`Could not clean up elevated process artifacts: ${errorMessage(error)}`);
    }
}
