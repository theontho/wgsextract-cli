import assert from "node:assert/strict";
import test from "node:test";

const { createProcessManager } = await import("../dist/web/src/server/process-runner.js");
const { TailBuffer } = await import("../dist/web/src/server/tail-buffer.js");
const { windowsAdminLauncherScript, windowsAdminWrapperScript } = await import("../dist/web/src/server/windows-admin.js");

const posixOnly = { skip: process.platform === "win32" ? "POSIX process groups" : false };

function processExists(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

async function waitFor(predicate, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  return predicate();
}

function firstLineCollector() {
  let text = "";
  let resolveLine;
  const line = new Promise((resolve) => {
    resolveLine = resolve;
  });
  return {
    line,
    onStdout(chunk) {
      text += chunk;
      const newline = text.indexOf("\n");
      if (newline >= 0) {
        resolveLine(text.slice(0, newline).trim());
      }
    },
  };
}

test("process manager captures successful output", async () => {
  const manager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  const result = await manager.runProcess(process.execPath, [
    "-e",
    "process.stdout.write('out'); process.stderr.write('err');",
  ], { env: process.env });

  assert.equal(result.exitCode, 0);
  assert.equal(result.stdout, "out");
  assert.equal(result.stderr, "err");
});

test("process manager keeps the tail of buffered stdout and stderr independently", async () => {
  const manager = createProcessManager({ maxOutputBytes: 3, maxErrorBytes: 2 });
  const stdoutChunks = [];
  const stderrChunks = [];

  const result = await manager.runProcess(process.execPath, [
    "-e",
    "process.stdout.write('abcdef'); process.stderr.write('wxyz');",
  ], {
    env: process.env,
    onStdout: (text) => stdoutChunks.push(text),
    onStderr: (text) => stderrChunks.push(text),
  });

  assert.equal(result.stdout, "def");
  assert.equal(result.stderr, "yz");
  assert.equal(result.stdoutTruncated, true);
  assert.equal(result.stderrTruncated, true);
  assert.equal(stdoutChunks.join(""), "abcdef");
  assert.equal(stderrChunks.join(""), "wxyz");
});

test("process manager rejects and terminates on timeout", async () => {
  const manager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  await assert.rejects(
    manager.runProcess(process.execPath, ["-e", "setInterval(() => {}, 1000);"], {
      env: process.env,
      timeoutMs: 100,
    }),
    /Process timed out after 1 seconds\./,
  );
});

test("process manager rejects aborted runs", async () => {
  const manager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });
  const abortController = new AbortController();
  const promise = manager.runProcess(process.execPath, ["-e", "setInterval(() => {}, 1000);"], {
    env: process.env,
    signal: abortController.signal,
  });

  abortController.abort();

  await assert.rejects(promise, /Process cancelled\./);
});

test("process manager keeps the last bytes of large output", async () => {
  const manager = createProcessManager({ maxOutputBytes: 1_000_000, maxErrorBytes: 65_536 });

  const result = await manager.runProcess(process.execPath, [
    "-e",
    "for (let i = 0; i < 300; i += 1) process.stdout.write(String(i).padStart(9, '0') + 'x'.repeat(9_990) + '\\n'); process.stdout.write('ERROR: last line');",
  ], { env: process.env });

  assert.equal(Buffer.byteLength(result.stdout), 1_000_000);
  assert.equal(result.stdoutTruncated, true);
  assert.ok(result.stdout.endsWith("ERROR: last line"));
});

test("tail buffer counts bytes and never starts inside a multi-byte character", () => {
  const buffer = new TailBuffer(5);
  buffer.push(Buffer.from("ééé", "utf8"));
  buffer.push(Buffer.from("é", "utf8"));

  assert.equal(buffer.truncated, true);
  assert.equal(buffer.text(), "éé");
  const exact = new TailBuffer(4);
  exact.push(Buffer.from("ab", "utf8"));
  exact.push(Buffer.from("cd", "utf8"));
  assert.equal(exact.truncated, false);
  assert.equal(exact.text(), "abcd");
});

test("process manager escalates cancelled SIGTERM-ignoring trees to SIGKILL", posixOnly, async () => {
  const manager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536, terminationGraceMs: 300 });
  const abortController = new AbortController();
  const collector = firstLineCollector();
  const run = manager.runProcess("/bin/sh", ["-c", "trap '' TERM; sleep 47 & echo $!; wait"], {
    env: process.env,
    signal: abortController.signal,
    onStdout: collector.onStdout,
  });
  const sleepPID = Number(await collector.line);
  assert.ok(processExists(sleepPID));

  const startedAt = Date.now();
  abortController.abort();

  await assert.rejects(run, /Process cancelled\./);
  assert.ok(Date.now() - startedAt < 2500, "cancel settles after the grace period");
  assert.ok(await waitFor(() => !processExists(sleepPID)), "the SIGTERM-ignoring child is gone");
});

test("terminateAllProcesses kills SIGTERM-ignoring trees", posixOnly, async () => {
  const manager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536, terminationGraceMs: 300 });
  const collector = firstLineCollector();
  const run = manager.runProcess("/bin/sh", ["-c", "trap '' TERM; sleep 47 & echo $!; wait"], {
    env: process.env,
    onStdout: collector.onStdout,
  });
  const sleepPID = Number(await collector.line);

  await manager.terminateAllProcesses();

  const result = await run;
  assert.notEqual(result.exitCode, 0);
  assert.ok(await waitFor(() => !processExists(sleepPID)));
});

test("process manager resolves when the direct child exits even if a descendant holds the pipes", posixOnly, async () => {
  const manager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536, terminationGraceMs: 300 });
  const startedAt = Date.now();

  const result = await manager.runProcess("/bin/sh", ["-c", "sleep 6 & echo $!; echo done"], { env: process.env });

  assert.ok(Date.now() - startedAt < 2000, `resolved after ${Date.now() - startedAt}ms`);
  assert.equal(result.exitCode, 0);
  const [backgroundPID, done] = result.stdout.trim().split("\n");
  assert.equal(done, "done");
  assert.ok(processExists(Number(backgroundPID)), "background descendant is still running");
  await manager.terminateAllProcesses();
  assert.ok(await waitFor(() => !processExists(Number(backgroundPID))), "lingering descendants are still tracked for shutdown");
});

test("Windows admin launcher script quotes arguments and keeps stderr encoding consistent", () => {
  const script = windowsAdminLauncherScript(
    { executable: "C:\\Tools\\run'it.exe", args: ["--name", "O'Brien", "C:\\Path With Spaces\\input.txt"] },
    { TOOL_HOME: "C:\\Tool's Home", REMOVE_ME: undefined },
    "C:\\Temp\\stdout.txt",
    "C:\\Temp\\stderr.txt",
    "C:\\Temp\\exit-code.txt",
  );

  assert.match(script, /Set-Item -LiteralPath 'Env:\\TOOL_HOME' -Value 'C:\\Tool''s Home'/);
  assert.match(script, /Remove-Item -LiteralPath 'Env:\\REMOVE_ME' -ErrorAction SilentlyContinue/);
  assert.match(script, /Start-Process -FilePath 'C:\\Tools\\run''it.exe' `/);
  assert.match(script, /-ArgumentList '--name O''Brien "C:\\Path With Spaces\\input.txt"' `/);
  assert.match(script, /-RedirectStandardOutput 'C:\\Temp\\stdout.txt' `/);
  assert.match(script, /-RedirectStandardError 'C:\\Temp\\stderr.txt' `/);
  assert.match(script, /\$process\.WaitForExit\(\)/);
  assert.doesNotMatch(script, /-Wait `/);
  // A failed command must not read as success: the handle keeps ExitCode readable, and a missing one fails.
  assert.match(script, /\$null = \$process\.Handle\n  \$process\.WaitForExit\(\)/);
  assert.match(script, /\$exitCode = if \(\$process\.ExitCode -is \[int\]\) { \$process\.ExitCode } else { .*; 1 }/);
  assert.doesNotMatch(script, /else { 0 }/);
  assert.match(script, /\$_ \| Out-File -FilePath 'C:\\Temp\\stderr.txt' -Append -Encoding utf8/);
  assert.match(script, /Set-Content -LiteralPath 'C:\\Temp\\exit-code.txt' -Value \$exitCode -Encoding ascii/);
});

test("Windows admin wrapper script quotes launcher path and streams output files", () => {
  const script = windowsAdminWrapperScript(
    "C:\\Temp\\launch 'quoted'.ps1",
    "C:\\Temp\\stdout.txt",
    "C:\\Temp\\stderr.txt",
    "C:\\Temp\\exit-code.txt",
    "C:\\Working Dir",
  );

  assert.match(script, /-ArgumentList '-NoProfile -ExecutionPolicy Bypass -File "C:\\Temp\\launch ''quoted''.ps1"'/);
  assert.match(script, /-WorkingDirectory 'C:\\Working Dir'/);
  assert.match(script, /function Write-NewFileContent/);
  assert.match(script, /\$fileEncoding = \[System\.Text\.UTF8Encoding\]::new\(\$false\)/);
  assert.match(script, /\$offset = if \(\$Position\.Value -eq 0 -and \$read -ge 3 -and \$buffer\[0\] -eq 0xef -and \$buffer\[1\] -eq 0xbb -and \$buffer\[2\] -eq 0xbf\) { 3 } else { 0 }/);
  assert.match(script, /Write-NewFileContent -Path 'C:\\Temp\\stdout.txt' -Position \(\[ref\]\$stdoutPosition\) -IsError \$false/);
  assert.match(script, /Write-NewFileContent -Path 'C:\\Temp\\stderr.txt' -Position \(\[ref\]\$stderrPosition\) -IsError \$true/);
  assert.match(script, /\$_ \| Out-File -FilePath 'C:\\Temp\\stderr.txt' -Append -Encoding utf8/);
  assert.match(script, /\$exitCode = if \(Test-Path -LiteralPath 'C:\\Temp\\exit-code.txt'\)/);
  assert.match(script, /elseif \(\$null -ne \$process -and \$process\.ExitCode -is \[int\]\) { \$process\.ExitCode } else { \[Console\]::Error\.WriteLine\('Admin command did not write an exit code\.'\); 1 }/);
});

test("Windows admin wrapper script supports scheduled task automation mode", () => {
  const script = windowsAdminWrapperScript(
    "C:\\Temp With Spaces\\launch.ps1",
    "C:\\Temp\\stdout.txt",
    "C:\\Temp\\stderr.txt",
    "C:\\Temp\\exit-code.txt",
    "C:\\Working Dir",
    {
      mode: "scheduled-task",
      taskName: "GUI For CLI Admin Broker",
      queueDirectory: "C:\\Temp Queue",
    },
  );

  assert.match(script, /\$requestDirectory = 'C:\\Temp Queue'/);
  assert.match(script, /launcherPath = 'C:\\Temp With Spaces\\launch\.ps1'/);
  assert.match(script, /workingDirectory = 'C:\\Working Dir'/);
  assert.match(script, /& schtasks\.exe \/Run \/TN 'GUI For CLI Admin Broker'/);
  assert.match(script, /\$adminCommandCompleted = Test-Path -LiteralPath 'C:\\Temp\\exit-code\.txt'/);
  assert.doesNotMatch(script, /-Verb RunAs/);
});

test("Windows admin wrapper script adds a deadline when a timeout is provided", () => {
  const script = windowsAdminWrapperScript(
    "C:\\Temp\\launch.ps1",
    "C:\\Temp\\stdout.txt",
    "C:\\Temp\\stderr.txt",
    "C:\\Temp\\exit-code.txt",
    undefined,
    {
      mode: "scheduled-task",
      taskName: "GUI For CLI Admin Broker",
      queueDirectory: "C:\\Temp Queue",
    },
    5_000,
  );

  assert.match(script, /\$deadline = \(Get-Date\)\.AddSeconds\(5\)/);
  assert.match(script, /Admin command did not complete before the process timeout\./);
});
