import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

const { platformCommand } = await import("../dist/web/src/server/platform-command.js");
const { runAction } = await import("../dist/web/src/server/action-runner.js");

test("routes POSIX Python scripts through python3", async (t) => {
  if (process.platform === "win32") {
    t.skip("POSIX command resolution is platform-specific.");
    return;
  }
  const script = path.resolve("..", "examples", "WGSExtract", "scripts", "posix", "test-genome-library.py");
  assert.deepEqual(await platformCommand(script, ["download"]), {
    executable: "python3",
    args: [script, "download"],
  });
});

test("reports resolved POSIX action script paths", async (t) => {
  if (process.platform === "win32") {
    t.skip("POSIX command resolution is platform-specific.");
    return;
  }
  const bundleRoot = path.resolve("..", "examples", "WGSExtract");
  const script = path.join(bundleRoot, "scripts", "posix", "test-genome-library.py");
  const calls = [];
  const events = [];

  const result = await runAction(
    {
      title: "Download Test Genome",
      command: {
        executable: "{{bundleRoot}}/scripts/test-genome-library.py",
        arguments: ["download", "{{genome_library}}"],
      },
    },
    {
      fieldValues: { genome_library: "/tmp/genomes" },
      checkedOptions: {},
      configValues: {},
      rowValues: {},
      bundleRootPath: bundleRoot,
    },
    new AbortController().signal,
    bundleRoot,
    async (executable, args) => {
      calls.push({ executable, args });
      events.push({ type: "process-started" });
      return { exitCode: 0, stdout: "", stderr: "" };
    },
    (event) => events.push(event),
  );

  assert.deepEqual(calls, [{ executable: script, args: ["download", "/tmp/genomes"] }]);
  assert.equal(result.command, `python3 ${script} download /tmp/genomes`);
  assert.equal(events[0].type, "start");
  assert.equal(events[0].command, `python3 ${script} download /tmp/genomes`);
  assert.equal(events.at(-1).type, "complete");
});

test("reports non-zero POSIX action results through complete events", async (t) => {
  if (process.platform === "win32") {
    t.skip("POSIX command resolution is platform-specific.");
    return;
  }
  const bundleRoot = path.resolve("..", "examples", "WGSExtract");
  const script = path.join(bundleRoot, "scripts", "posix", "test-genome-library.py");
  const calls = [];
  const events = [];

  const result = await runAction(
    {
      title: "Download Test Genome",
      command: {
        executable: "{{bundleRoot}}/scripts/test-genome-library.py",
        arguments: ["download", "{{genome_library}}"],
      },
    },
    {
      fieldValues: { genome_library: "/tmp/genomes" },
      checkedOptions: {},
      configValues: {},
      rowValues: {},
      bundleRootPath: bundleRoot,
    },
    new AbortController().signal,
    bundleRoot,
    async (executable, args) => {
      calls.push({ executable, args });
      return { exitCode: 2, stdout: "", stderr: "boom" };
    },
    (event) => events.push(event),
  );

  assert.deepEqual(calls, [{ executable: script, args: ["download", "/tmp/genomes"] }]);
  assert.equal(result.exitCode, 2);
  assert.equal(result.stderr, "boom");
  assert.equal(result.command, `python3 ${script} download /tmp/genomes`);
  assert.equal(events[0].type, "start");
  assert.equal(events.at(-1).type, "complete");
  assert.equal(events.at(-1).result.exitCode, 2);
});

test("uses Windows executables for Unix setup helpers", async (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows command resolution is platform-specific.");
    return;
  }

  assert.deepEqual(await platformCommand("/usr/bin/env", ["which", "pixi"]), {
    executable: "where.exe",
    args: ["pixi"],
  });
  assert.deepEqual(await platformCommand("/usr/bin/env", ["pixi", "install"]), {
    executable: "pixi",
    args: ["install"],
  });
});

test("checks Windows pathTool absolute paths without where.exe", async (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows command resolution is platform-specific.");
    return;
  }
  const target = path.resolve("..", "examples", "WGSExtract", "runtime", "wgsextract-cli", "bin", "wgsextract");
  const result = await platformCommand("/usr/bin/env", ["which", target]);

  assert.equal(result.executable, "powershell.exe");
  assert.deepEqual(result.args.slice(0, 4), ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]);
  assert.match(result.args[4], /\$candidate = '/);
  assert.match(result.args[4], /Test-Path -LiteralPath/);
  assert.match(result.args[4], /\.cmd/);
  assert.equal(result.args.length, 5);
});

test("routes Windows script files through platform interpreters", async (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows command resolution is platform-specific.");
    return;
  }
  const script = path.resolve("..", "examples", "WGSExtract", "scripts", "windows", "run-wgsextract.ps1");
  const result = await platformCommand(script, ["deps", "check"]);

  assert.equal(result.executable, "powershell.exe");
  assert.deepEqual(result.args.slice(0, 5), ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]);
  assert.equal(result.args[5], script);
  assert.deepEqual(result.args.slice(6), ["deps", "check"]);
  assert.deepEqual(await platformCommand(path.resolve("..", "examples", "WGSExtract", "scripts", "shared", "list_reference_genomes.py"), ["options"]), {
    executable: "python",
    args: [path.resolve("..", "examples", "WGSExtract", "scripts", "shared", "list_reference_genomes.py"), "options"],
  });
});
