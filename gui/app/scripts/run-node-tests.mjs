import { spawn } from "node:child_process";

const defaultTests = [
  "tests/dev-runner.test.mjs",
  "tests/rendering.test.mjs",
  "tests/rendering-regressions.test.mjs",
  "tests/bundle-loader.test.mjs",
  "tests/manifest-validator.test.mjs",
  "tests/bundle-archive.test.mjs",
  "tests/paths.test.mjs",
  "tests/config-store.test.mjs",
  "tests/bundle-test-runner.test.mjs",
  "tests/bundle-uninstall-cli.test.mjs",
  "tests/example-bundles.test.mjs",
  "tests/workspace.test.mjs",
  "tests/platform.test.mjs",
  "tests/platform-command.test.mjs",
  "tests/process-runner.test.mjs",
  "tests/path-picker.test.mjs",
  "tests/operations.test.mjs",
  "tests/routes.test.mjs",
  "tests/setup-runner.test.mjs",
  "tests/tauri-updater.test.mjs",
  "tests/developer-cache.test.mjs",
  "tests/terminal.test.mjs",
  "tests/conformance.test.mjs",
];

const tests = process.argv.slice(2);
const child = spawn(process.execPath, ["--test", ...(tests.length > 0 ? tests : defaultTests)], {
  stdio: ["ignore", "pipe", "pipe"],
  windowsHide: true,
});

const stdout = [];
const stderr = [];
child.stdout.on("data", (chunk) => stdout.push(chunk));
child.stderr.on("data", (chunk) => stderr.push(chunk));

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    child.kill(signal);
  });
}

child.on("close", (code, signal) => {
  const stdoutText = Buffer.concat(stdout).toString("utf8");
  const stderrText = Buffer.concat(stderr).toString("utf8");
  if (signal) {
    writeBufferedOutput(stdoutText, stderrText);
    process.kill(process.pid, signal);
    return;
  }
  if (code === 0) {
    console.log(successSummary(stdoutText));
  } else {
    writeBufferedOutput(stdoutText, stderrText);
  }
  process.exit(code ?? 1);
});

child.on("error", (error) => {
  console.error(error);
  process.exit(1);
});

function successSummary(output) {
  const total = output.match(/tests\s+(\d+)/)?.[1];
  const passed = output.match(/pass\s+(\d+)/)?.[1];
  const skipped = output.match(/skipped\s+(\d+)/)?.[1];
  const duration = output.match(/duration_ms\s+([\d.]+)/)?.[1];
  const parts = [];
  if (passed) {
    parts.push(`${passed} passed`);
  }
  if (skipped && skipped !== "0") {
    parts.push(`${skipped} skipped`);
  }
  if (total) {
    parts.push(`${total} total`);
  }
  if (duration) {
    parts.push(`${Math.round(Number(duration))}ms`);
  }
  return parts.length > 0 ? `Node tests passed (${parts.join(", ")}).` : "Node tests passed.";
}

function writeBufferedOutput(stdoutText, stderrText) {
  if (stdoutText.length > 0) {
    process.stdout.write(stdoutText);
  }
  if (stderrText.length > 0) {
    process.stderr.write(stderrText);
  }
}
