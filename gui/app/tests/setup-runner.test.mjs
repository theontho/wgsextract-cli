import assert from "node:assert/strict";
import { chmod, cp, mkdir, mkdtemp, readFile, realpath, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import test from "node:test";
import { currentSetupPlatform } from "../dist/shared/setup-platforms.js";
import { runInitialSetupIfNeeded, runSetup, runSetupStep, runUninstall } from "../dist/web/src/server/setup-runner.js";
import { createProcessManager } from "../dist/web/src/server/process-runner.js";

test("normalizes setup platform aliases", () => {
  assert.equal(currentSetupPlatform("darwin"), "macos");
  assert.equal(currentSetupPlatform("mac"), "macos");
  assert.equal(currentSetupPlatform("win"), "windows");
  assert.equal(currentSetupPlatform("win32"), "windows");
  assert.equal(currentSetupPlatform("linux"), "linux");
  assert.equal(currentSetupPlatform("haiku"), "posix");
});

test("setup steps run without a wall-clock timeout and stop on cancellation", async () => {
  const calls = [];
  const controller = new AbortController();
  const manifest = {
    setup: {
      steps: [
        { id: "install", kind: "pixiInstall", label: "Install" },
        { id: "check", kind: "pathTool", label: "Check", value: "node" },
      ],
    },
  };
  const runProcess = async (executable, args, options) => {
    calls.push({ executable, args, options });
    controller.abort();
    throw new Error("Process cancelled.");
  };

  await assert.rejects(runSetup(manifest, tmpdir(), runProcess, () => {}, {}, controller.signal), /Process cancelled/);

  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.timeoutMs, undefined);
  assert.equal(calls[0].options.signal, controller.signal);
});

test("setup and uninstall share one lock per workspace", async () => {
  const workspace = await mkdtemp(path.join(tmpdir(), "gui-for-cli-setup-lock-"));
  let release;
  const blocked = new Promise((resolve) => {
    release = resolve;
  });
  const manifest = {
    setup: { steps: [{ id: "install", kind: "pixiInstall", label: "Install" }] },
    uninstall: { steps: [{ id: "remove", kind: "pathTool", label: "Remove", value: "node" }] },
  };
  const runProcess = async () => {
    await blocked;
    return { exitCode: 0, stdout: "", stderr: "" };
  };
  try {
    const first = runSetup(manifest, workspace, runProcess);
    await assert.rejects(runSetup(manifest, workspace, runProcess), /already running for this bundle workspace/);
    await assert.rejects(runUninstall(manifest, `${workspace}${path.sep}`, runProcess), /already running/);
    release();
    assert.equal((await first).status, "ok");
    assert.equal((await runUninstall(manifest, workspace, runProcess)).status, "ok");
  }
  finally {
    await rm(workspace, { force: true, recursive: true });
  }
});

test("runs only the requested setup step", async () => {
  const calls = [];
  const manifest = {
    setup: {
      steps: [
        { id: "pixi", kind: "pathTool", label: "Find Pixi", value: "pixi" },
          {
            id: "deps",
            kind: "pixiRun",
            label: "Check deps",
            value: "wgsextract",
            arguments: ["deps", "check"],
            workingDirectory: "runtime/wgsextract-cli/app",
          },
      ],
    },
  };
  const runProcess = async (executable, args, options) => {
    calls.push({ executable, args, options });
    return { exitCode: 0, stdout: "ok\n", stderr: "" };
  };
  const bundleRoot = path.resolve("bundle");

  const result = await runSetupStep(manifest, bundleRoot, runProcess, "deps");

  assert.equal(calls.length, 1);
  assert.equal(calls[0].executable, process.platform === "win32" ? "pixi" : "/usr/bin/env");
  assert.deepEqual(calls[0].args, process.platform === "win32"
    ? ["run", "wgsextract", "deps", "check"]
    : ["pixi", "run", "wgsextract", "deps", "check"]);
  assert.equal(calls[0].options.cwd, path.join(bundleRoot, "runtime", "wgsextract-cli", "app"));
  assert.equal(Object.hasOwn(calls[0].options, "elevatedEnv"), false);
  assert.equal(result.id, "deps");
  assert.equal(result.status, "ok");
  assert.equal(result.stdout, "ok\n");
  assert.equal(Number.isFinite(result.durationMs), true);
});

test("wraps admin setup steps with elevated execution", async (t) => {
  const calls = [];
  const previousParentEnv = process.env.GUI_FOR_CLI_TEST_PARENT_ENV;
  process.env.GUI_FOR_CLI_TEST_PARENT_ENV = "parent-value";
  t.after(() => {
    if (previousParentEnv == null) {
      delete process.env.GUI_FOR_CLI_TEST_PARENT_ENV;
    } else {
      process.env.GUI_FOR_CLI_TEST_PARENT_ENV = previousParentEnv;
    }
  });
  const manifest = {
    setup: {
      steps: [
        {
          id: "admin",
          kind: "setupScript",
          label: "Admin install",
          value: "scripts/admin.sh",
          requiresAdmin: true,
          environment: {
            SETUP_VALUE: "needs spaces",
            TOOL_HOME: "{{bundleRoot}}/runtime/tool",
          },
        },
      ],
    },
  };
  const bundleRoot = path.resolve("bundle");
  const runProcess = async (executable, args, options) => {
    calls.push({ executable, args, options });
    return { exitCode: 0, stdout: "ok\n", stderr: "" };
  };

  const result = await runSetupStep(manifest, bundleRoot, runProcess, "admin");

  assert.equal(result.status, "ok");
  assert.equal(calls.length, 1);
  if (process.platform === "win32") {
    assert.equal(result.command.startsWith("sudo "), false);
    assert.equal(calls[0].options.requiresAdmin, true);
    assert.equal(calls[0].options.elevatedEnv.GUI_FOR_CLI_TEST_PARENT_ENV, "parent-value");
    assert.equal(calls[0].options.elevatedEnv.SETUP_VALUE, "needs spaces");
    assert.equal(calls[0].options.elevatedEnv.TOOL_HOME, path.join(bundleRoot, "runtime", "tool"));
    assert.equal(calls[0].options.elevatedEnv.GUI_FOR_CLI_BUNDLE_ROOT, bundleRoot);
    assert.equal(calls[0].options.elevatedEnv.GUI_FOR_CLI_BUNDLE_WORKSPACE, bundleRoot);
  } else if (process.platform === "darwin") {
    assert.equal(result.command.startsWith("sudo "), true);
    assert.equal(calls[0].executable, "/usr/bin/osascript");
    assert.equal(calls[0].options.cwd, undefined);
    assert.match(calls[0].args[1], /with administrator privileges/);
    assert.match(calls[0].args[1], /'SETUP_VALUE=needs spaces'/);
    assert.match(calls[0].args[1], /GUI_FOR_CLI_BUNDLE_ROOT=/);
  } else {
    assert.equal(result.command.startsWith("sudo "), true);
    assert.equal(calls[0].executable, "/usr/bin/sudo");
    assert.equal(calls[0].args[0], "/usr/bin/env");
    assert.equal(calls[0].args.includes("SETUP_VALUE=needs spaces"), true);
    assert.equal(calls[0].args.includes(`TOOL_HOME=${path.join(bundleRoot, "runtime", "tool")}`), true);
    assert.equal(calls[0].args.some((argument) => argument.startsWith("GUI_FOR_CLI_BUNDLE_ROOT=")), true);
    assert.equal(calls[0].args.at(-1), path.join(bundleRoot, "scripts", "admin.sh"));
    assert.equal(calls[0].options.cwd, bundleRoot);
  }
});

test("skips setup steps for other platforms", async () => {
  const calls = [];
  const otherPlatform = process.platform === "darwin" ? "windows" : "macos";
  const manifest = {
    setup: {
      steps: [
        { id: "platform-only", kind: "pathTool", label: "Other Platform", value: "other-tool", platforms: [otherPlatform] },
        { id: "portable", kind: "pathTool", label: "Portable", value: "portable-tool" },
      ],
    },
  };
  const runProcess = async (executable, args, options) => {
    calls.push({ executable, args, options });
    return { exitCode: 0, stdout: "ok\n", stderr: "" };
  };

  const result = await runSetup(manifest, path.resolve("bundle"), runProcess);

  assert.equal(result.status, "ok");
  assert.deepEqual(result.results.map((step) => step.id), ["portable"]);
  assert.equal(calls.length, 1);
  assert.ok(calls[0].args.includes("portable-tool"));
});

test("uses Windows equivalents for setup commands", async (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows command resolution is platform-specific.");
    return;
  }
  const calls = [];
  const manifest = {
    setup: {
      steps: [
        { id: "pixi", kind: "pathTool", label: "Pixi", value: "pixi" },
        { id: "script", kind: "setupScript", label: "Script", value: "scripts/setup-wgsextract-pixi.sh" },
      ],
    },
  };
  const bundleRoot = path.resolve("..", "examples", "WGSExtract");
  const runProcess = async (executable, args, options) => {
    calls.push({ executable, args, options });
    return { exitCode: 0, stdout: "", stderr: "" };
  };

  await runSetupStep(manifest, bundleRoot, runProcess, "pixi");
  await runSetupStep(manifest, bundleRoot, runProcess, "script");

  assert.equal(calls[0].executable, "where.exe");
  assert.deepEqual(calls[0].args, ["pixi"]);
  assert.equal(calls[1].executable, "powershell.exe");
  assert.deepEqual(calls[1].args.slice(0, 5), ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]);
  assert.equal(calls[1].args[5], path.join(bundleRoot, "scripts", "windows", "setup-wgsextract-pixi.ps1"));
});

test("WGSExtract platform script folders have complete script sets", async () => {
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const bundleRoot = path.join(repoRoot, "examples", "WGSExtract");
  const manifest = (await import("../dist/web/src/server/bundle-loader.js")).loadManifestFromRoot;
  await manifest(bundleRoot);
});

test("WGSExtract keeps platform scripts out of the shared script root", async () => {
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const scriptsRoot = path.join(repoRoot, "examples", "WGSExtract", "scripts");
  const scriptExtensions = new Set([".sh", ".ps1", ".py"]);
  const rootScripts = (await readdir(scriptsRoot, { withFileTypes: true }))
    .filter((entry) => entry.isFile() && scriptExtensions.has(path.extname(entry.name)))
    .map((entry) => path.basename(entry.name, path.extname(entry.name)));
  const platformScripts = new Set();

  for (const directoryName of ["posix", "windows"]) {
    const directory = path.join(scriptsRoot, directoryName);
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      if (entry.isFile() && scriptExtensions.has(path.extname(entry.name))) {
        platformScripts.add(path.basename(entry.name, path.extname(entry.name)));
      }
    }
  }

  assert.deepEqual(rootScripts.filter((script) => platformScripts.has(script)).sort(), []);
});

test("WGSExtract Windows library state delegates to CLI status", async (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows data source behavior is platform-specific.");
    return;
  }

  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const sourceScript = path.join(repoRoot, "examples", "WGSExtract", "scripts", "windows", "library-state.ps1");
  const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-wgsextract-library-state-"));
  const scriptRoot = path.join(tempRoot, "scripts");
  const script = path.join(scriptRoot, "library-state.ps1");
  const refPath = path.join(tempRoot, "reference");
  const genomeLibrary = path.join(tempRoot, "genomes");
  const annotationVcf = path.join(tempRoot, "annotation.vcf.gz");
  const inputVcf = path.join(tempRoot, "input.hg38.vcf.gz");
  const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  try {
    await mkdir(scriptRoot, { recursive: true });
    await cp(sourceScript, script);
    await writeFile(path.join(scriptRoot, "run-wgsextract.ps1"), [
      "Set-Content -LiteralPath (Join-Path $PSScriptRoot 'calls.log') -Encoding utf8 -Value ($args -join '|')",
      "'{\"values\":{\"library.testGenomeStatus\":\"from-cli\"}}'",
      "exit 0",
      "",
    ].join("\r\n"));
    const result = await processManager.runProcess("powershell.exe", [
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      script,
      refPath,
      genomeLibrary,
      annotationVcf,
      inputVcf,
    ], {});
    assert.equal(result.exitCode, 0, result.stderr);
    const state = JSON.parse(result.stdout);
    assert.equal(state.values["library.testGenomeStatus"], "from-cli");
    const call = await readFile(path.join(scriptRoot, "calls.log"), "utf8");
    assert.match(call, new RegExp(`ref\\|status\\|--values\\|--ref\\|${escapeRegExp(refPath)}`));
    assert.match(call, new RegExp(`--genome-library\\|${escapeRegExp(genomeLibrary)}`));
    assert.match(call, new RegExp(`--annotation-vcf\\|${escapeRegExp(annotationVcf)}`));
    assert.match(call, new RegExp(`--input\\|${escapeRegExp(inputVcf)}`));
  } finally {
    await processManager.terminateAllProcesses();
    await rm(tempRoot, { force: true, recursive: true });
  }
});

test("WGSExtract Windows config bootstrap includes genome library default", async (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows config bootstrap behavior is platform-specific.");
    return;
  }

  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const script = path.join(repoRoot, "examples", "WGSExtract", "scripts", "windows", "bootstrap-wgsextract-config.ps1");
  const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-wgsextract-config-"));
  const previousWorkspace = process.env.GUI_FOR_CLI_BUNDLE_WORKSPACE;
  const previousConfigPath = process.env.GUI_FOR_CLI_CONFIG_PATH;
  const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  try {
    process.env.GUI_FOR_CLI_BUNDLE_WORKSPACE = tempRoot;
    delete process.env.GUI_FOR_CLI_CONFIG_PATH;
    const result = await processManager.runProcess("powershell.exe", [
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      script,
    ], { env: process.env });
    assert.equal(result.exitCode, 0, result.stderr);
    const bootstrap = JSON.parse(result.stdout);

    assert.equal(bootstrap.path, path.join(tempRoot, "settings", "config.toml"));
    assert.match(bootstrap.contents, new RegExp(`output_directory = "${escapeRegExp(tomlBasicStringValue(path.join(tempRoot, "output")))}"`));
    assert.match(bootstrap.contents, new RegExp(`reference_library = "${escapeRegExp(tomlBasicStringValue(path.join(tempRoot, "reference")))}"`));
    assert.match(bootstrap.contents, new RegExp(`genome_library = "${escapeRegExp(tomlBasicStringValue(path.join(tempRoot, "genomes")))}"`));
  } finally {
    await processManager.terminateAllProcesses();
    setOrDeleteEnv("GUI_FOR_CLI_BUNDLE_WORKSPACE", previousWorkspace);
    setOrDeleteEnv("GUI_FOR_CLI_CONFIG_PATH", previousConfigPath);
    await rm(tempRoot, { force: true, recursive: true });
  }
});

test("WGSExtract Windows repair actions forward to CLI file mode", async (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows PowerShell wrapper behavior is platform-specific.");
    return;
  }

  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const scriptsRoot = path.join(repoRoot, "examples", "WGSExtract", "scripts", "windows");
  const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-wgsextract-repair-"));
  const appDir = path.join(tempRoot, "runtime", "app");
  const outDir = path.join(tempRoot, "out");
  const fakePixi = path.join(tempRoot, "pixi.ps1");
  const vcfInput = path.join(tempRoot, "sample.vcf");
  const bamInput = path.join(tempRoot, "sample.bam");
  const configPath = path.join(tempRoot, "settings", "config.toml");
  const previousPixi = process.env.PIXI;
  const previousAppDir = process.env.WGSEXTRACT_APP_DIR;
  const previousConfigPath = process.env.GUI_FOR_CLI_CONFIG_PATH;
  const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  try {
    await mkdir(appDir, { recursive: true });
    await mkdir(outDir, { recursive: true });
    await writeFile(vcfInput, "original-vcf\n");
    await writeFile(bamInput, "original-bam\n");
    await writeFile(fakePixi, [
      "param([Parameter(Position=0, ValueFromRemainingArguments=$true)][string[]]$Rest, [Parameter(ValueFromPipeline=$true)]$PipelineInput)",
      "begin { $stdinLines = @() }",
      "process { if ($null -ne $PipelineInput) { $stdinLines += [string]$PipelineInput } }",
      "end {",
      "  $log = Join-Path $PSScriptRoot 'calls.log'",
      "  Add-Content -LiteralPath $log -Value ('PIXI|' + ($Rest -join '|') + '|STDIN=' + ($stdinLines -join '\\n'))",
      "  if ($Rest.Count -ge 5 -and $Rest[0] -eq 'run' -and $Rest[1] -eq 'wgsextract' -and $Rest[2] -eq '--config' -and $Rest[4] -eq 'repair') {",
      "    foreach ($line in $stdinLines) { Write-Output ('REPAIRED:' + $line) }",
      "    exit 0",
      "  }",
      "  Write-Output ('fake pixi ' + ($Rest -join ' '))",
      "  exit 0",
      "}",
      "",
    ].join("\r\n"));
    process.env.PIXI = fakePixi;
    process.env.WGSEXTRACT_APP_DIR = appDir;
    process.env.GUI_FOR_CLI_CONFIG_PATH = configPath;

    const vcfResult = await processManager.runProcess("powershell.exe", [
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      path.join(scriptsRoot, "run-wgsextract.ps1"),
      "repair",
      "ftdna-vcf",
      "--input",
      vcfInput,
      "--outdir",
      outDir,
    ], { env: process.env });
    assert.equal(vcfResult.exitCode, 0, vcfResult.stderr);

    const bamResult = await processManager.runProcess("powershell.exe", [
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      path.join(scriptsRoot, "run-wgsextract.ps1"),
      "repair",
      "ftdna-bam",
      "--input",
      bamInput,
      "--outdir",
      outDir,
    ], { env: process.env });
    assert.equal(bamResult.exitCode, 0, bamResult.stderr);

    const calls = await readFile(path.join(tempRoot, "calls.log"), "utf8");
    assert.match(calls, new RegExp(`PIXI\\|run\\|wgsextract\\|--config\\|${escapeRegExp(configPath)}\\|repair\\|ftdna-vcf\\|--input\\|${escapeRegExp(vcfInput)}\\|--outdir\\|${escapeRegExp(outDir)}\\|STDIN=`));
    assert.match(calls, new RegExp(`PIXI\\|run\\|wgsextract\\|--config\\|${escapeRegExp(configPath)}\\|repair\\|ftdna-bam\\|--input\\|${escapeRegExp(bamInput)}\\|--outdir\\|${escapeRegExp(outDir)}\\|STDIN=`));
  } finally {
    await processManager.terminateAllProcesses();
    setOrDeleteEnv("PIXI", previousPixi);
    setOrDeleteEnv("WGSEXTRACT_APP_DIR", previousAppDir);
    setOrDeleteEnv("GUI_FOR_CLI_CONFIG_PATH", previousConfigPath);
    await rm(tempRoot, { force: true, recursive: true });
  }
});

test("WGSExtract Windows runtime wrapper does not wait for stdin unless requested", async (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows PowerShell wrapper behavior is platform-specific.");
    return;
  }

  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const sourceScripts = path.join(repoRoot, "examples", "WGSExtract", "scripts", "windows");
  const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-wgsextract-no-stdin-"));
  const scriptsRoot = path.join(tempRoot, "scripts");
  const appDir = path.join(tempRoot, "app");
  const fakePixi = path.join(tempRoot, "pixi.cmd");
  const previousPixi = process.env.PIXI;
  const previousAppDir = process.env.WGSEXTRACT_APP_DIR;
  const previousForwardStdin = process.env.WGSEXTRACT_FORWARD_STDIN;
  const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  try {
    await cp(sourceScripts, scriptsRoot, { recursive: true });
    await mkdir(appDir, { recursive: true });
    await writeFile(fakePixi, [
      "@echo off",
      `>> "${path.join(tempRoot, "calls.log")}" echo PIXI^|%*`,
      "exit /b 0",
      "",
    ].join("\r\n"));
    process.env.PIXI = fakePixi;
    process.env.WGSEXTRACT_APP_DIR = appDir;
    delete process.env.WGSEXTRACT_FORWARD_STDIN;

    const result = await processManager.runProcess(path.join(scriptsRoot, "run-wgsextract-env.ps1"), [
      "wgsextract",
      "ref",
      "list",
    ], { env: process.env, timeoutMs: 5_000 });

    assert.equal(result.exitCode, 0, result.stderr);
    const calls = await readFile(path.join(tempRoot, "calls.log"), "utf8");
    assert.match(calls, /PIXI\|run wgsextract ref list/);
  } finally {
    await processManager.terminateAllProcesses();
    setOrDeleteEnv("PIXI", previousPixi);
    setOrDeleteEnv("WGSEXTRACT_APP_DIR", previousAppDir);
    setOrDeleteEnv("WGSEXTRACT_FORWARD_STDIN", previousForwardStdin);
    await rm(tempRoot, { force: true, recursive: true });
  }
});

test("WGSExtract Windows runtime wrapper rejects BAM and CRAM index inputs", async (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows PowerShell wrapper behavior is platform-specific.");
    return;
  }

  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const scriptsRoot = path.join(repoRoot, "examples", "WGSExtract", "scripts", "windows");
  const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  try {
    const result = await processManager.runProcess("powershell.exe", [
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      path.join(scriptsRoot, "run-wgsextract.ps1"),
      "microarray",
      "--input",
      "sample.cram.crai",
    ], { env: process.env });

    assert.equal(result.exitCode, 1);
    assert.match(result.stderr, /Selected CRAM index file/);
    assert.match(result.stderr, /Choose the CRAM data file instead: sample\.cram(\s|$)/);
  } finally {
    await processManager.terminateAllProcesses();
  }
});

test("WGSExtract POSIX runtime wrapper rejects BAM and CRAM index inputs", async (t) => {
  if (process.platform === "win32") {
    t.skip("POSIX shell wrapper behavior is covered on non-Windows platforms.");
    return;
  }

  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const script = path.join(repoRoot, "examples", "WGSExtract", "scripts", "posix", "run-wgsextract.sh");
  const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  try {
    const cramResult = await processManager.runProcess("sh", [
      script,
      "microarray",
      "--input",
      "sample.CRAM.CRAI",
    ], { env: process.env });
    assert.equal(cramResult.exitCode, 1);
    assert.match(cramResult.stderr, /Selected CRAM index file/);
    assert.match(cramResult.stderr, /Choose the CRAM data file instead: sample\.CRAM(\s|$)/);

    const bamResult = await processManager.runProcess("sh", [
      script,
      "microarray",
      "--input=sample.BAM.BAI",
    ], { env: process.env });
    assert.equal(bamResult.exitCode, 1);
    assert.match(bamResult.stderr, /Selected BAM index file/);
    assert.match(bamResult.stderr, /Choose the BAM data file instead: sample\.BAM(\s|$)/);

    const missingRefPath = path.join(
      await mkdtemp(path.join(tmpdir(), "missing-wgsextract-reference-")),
      "reference.fa",
    );
    const refResult = await processManager.runProcess("sh", [
      script,
      "microarray",
      "--input=sample.bam",
      "--ref",
      missingRefPath,
    ], { env: process.env });
    assert.equal(refResult.exitCode, 1);
    assert.match(refResult.stderr, /Reference genome was not found/);
    assert.match(refResult.stderr, /Library page or rerun setup/);

    const directoryRefPath = await mkdtemp(path.join(tmpdir(), "wgsextract-reference-library-"));
    const directoryRefResult = await processManager.runProcess("sh", [
      script,
      "microarray",
      "--input=sample.bam",
      "--ref",
      directoryRefPath,
    ], { env: process.env });
    assert.equal(directoryRefResult.exitCode, 1);
    assert.match(directoryRefResult.stderr, /must be a FASTA file/);
    assert.match(directoryRefResult.stderr, /Reference genome dropdown/);
  } finally {
    await processManager.terminateAllProcesses();
  }
});

test("WGSExtract Windows reference delete treats missing files as no-op", async (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows delete script behavior is platform-specific.");
    return;
  }

  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const script = path.join(repoRoot, "examples", "WGSExtract", "scripts", "windows", "delete-reference-genome.ps1");
  const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-wgsextract-delete-ref-"));
  const reference = path.join(tempRoot, "reference");
  const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  try {
    await mkdir(path.join(reference, "genomes"), { recursive: true });
    const result = await processManager.runProcess("powershell.exe", [
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      script,
      reference,
      "missing.fa",
    ], {});
    assert.equal(result.exitCode, 0, result.stderr);
    assert.match(`${result.stdout}\n${result.stderr}`, /No files found/);
  } finally {
    await processManager.terminateAllProcesses();
    await rm(tempRoot, { force: true, recursive: true });
  }
});

test("WGSExtract Windows VCF wrapper adds the FASTA build's ploidy", async (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows PowerShell wrapper behavior is platform-specific.");
    return;
  }

  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const sourceScripts = path.join(repoRoot, "examples", "WGSExtract", "scripts", "windows");
  const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-wgsextract-vcf-wrapper-"));
  const scriptsRoot = path.join(tempRoot, "scripts");
  const genomeRoot = path.join(tempRoot, "genomes", "wgsextract-benchmark-hg19-mini");
  const referenceRoot = path.join(tempRoot, "reference");
  const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });
  const runWrapper = (ref) => processManager.runProcess("powershell.exe", [
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    path.join(scriptsRoot, "run-wgsextract-vcf.ps1"),
    "snp",
    "--input",
    path.join(genomeRoot, "HG00096.hg19-mini.cram"),
    "--ref",
    ref,
  ], {});

  try {
    await cp(sourceScripts, scriptsRoot, { recursive: true });
    await mkdir(genomeRoot, { recursive: true });
    await mkdir(referenceRoot, { recursive: true });
    const fastaPath = path.join(genomeRoot, "hg19-mini.fa.gz");
    await writeFile(fastaPath, "");
    await writeFile(path.join(scriptsRoot, "run-wgsextract.ps1"), [
      "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Rest)",
      "Set-Content -LiteralPath (Join-Path $PSScriptRoot 'calls.log') -Encoding utf8 -Value ($Rest -join '|')",
      "exit 0",
      "",
    ].join("\r\n"));
    const callsLog = path.join(scriptsRoot, "calls.log");

    let result = await runWrapper(fastaPath);
    assert.equal(result.exitCode, 0, result.stderr);
    let call = await readFile(callsLog, "utf8");
    assert.match(call, new RegExp(`vcf\\|snp\\|.*--ref\\|${escapeRegExp(fastaPath)}\\|--ploidy\\|GRCh37`));

    // A reference library resolves its own ploidy file in the CLI.
    result = await runWrapper(referenceRoot);
    assert.equal(result.exitCode, 0, result.stderr);
    call = await readFile(callsLog, "utf8");
    assert.match(call, new RegExp(`--ref\\|${escapeRegExp(referenceRoot)}`));
    assert.doesNotMatch(call, /--ploidy/);
  } finally {
    await processManager.terminateAllProcesses();
    await rm(tempRoot, { force: true, recursive: true });
  }
});

test("runs WGSExtract POSIX setup scripts from nested script folders", async (t) => {
  if (process.platform === "win32") {
    t.skip("This regression covers POSIX packaged setup script paths.");
    return;
  }

  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const sourceBundleRoot = path.join(repoRoot, "examples", "WGSExtract");
  const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-wgsextract-posix-setup-"));
  const bundleRoot = path.join(tempRoot, "WGSExtract");
  const appDir = path.join(bundleRoot, "runtime", "wgsextract-cli", "app");
  const fakePixi = path.join(tempRoot, "pixi");
  const previousPixi = process.env.PIXI;
  const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  try {
    await cp(sourceBundleRoot, bundleRoot, { recursive: true });
    await rm(path.join(bundleRoot, "reference"), { force: true, recursive: true });
    await mkdir(appDir, { recursive: true });
    await writeFile(fakePixi, "#!/bin/sh\necho fake pixi \"$@\"\nexit 0\n");
    await chmod(fakePixi, 0o755);
    process.env.PIXI = fakePixi;

    const { loadManifestFromRoot } = await import("../dist/web/src/server/bundle-loader.js");
    const manifest = await loadManifestFromRoot(bundleRoot);
    const result = await runSetupStep(
      manifest,
      bundleRoot,
      processManager.runProcess,
      "bootstrap-reference-library",
    );

    assert.equal(result.status, "ok", [result.stdout, result.stderr].filter(Boolean).join("\n"));
    assert.match(result.command, /scripts\/posix\/bootstrap-reference-library\.sh/);
    assert.match(result.stdout, /fake pixi run wgsextract --config .*settings[\\/]config\.toml ref bootstrap --ref /);
  } finally {
    await processManager.terminateAllProcesses();
    setOrDeleteEnv("PIXI", previousPixi);
    await rm(tempRoot, { force: true, recursive: true });
  }
});

test("runs WGSExtract platform setup scripts from nested script folders", async (t) => {
  if (process.platform !== "win32") {
    t.skip("This regression covers the packaged Windows setup script path.");
    return;
  }

  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const bundleRoot = path.join(repoRoot, "examples", "WGSExtract");
  const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-wgsextract-setup-"));
  const appDir = path.join(tempRoot, "runtime", "wgsextract-cli", "app");
  const referenceLibrary = path.join(tempRoot, "reference");
  const fakePixi = path.join(tempRoot, "pixi.cmd");
  const previousPixi = process.env.PIXI;
  const previousAppDir = process.env.WGSEXTRACT_APP_DIR;
  const previousReferenceLibrary = process.env.WGSEXTRACT_REFERENCE_LIBRARY;
  const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  try {
    await mkdir(appDir, { recursive: true });
    await writeFile(fakePixi, [
      "@echo off",
      "echo fake pixi %*",
      "exit /b 0",
      "",
    ].join("\r\n"));
    await chmod(fakePixi, 0o755);
    process.env.PIXI = fakePixi;
    process.env.WGSEXTRACT_APP_DIR = appDir;
    process.env.WGSEXTRACT_REFERENCE_LIBRARY = referenceLibrary;

    const { loadManifestFromRoot } = await import("../dist/web/src/server/bundle-loader.js");
    const manifest = await loadManifestFromRoot(bundleRoot);
    const result = await runSetupStep(
      manifest,
      bundleRoot,
      processManager.runProcess,
      "bootstrap-reference-library",
    );

    assert.equal(result.status, "ok", [result.stdout, result.stderr].filter(Boolean).join("\n"));
    assert.match(result.command, /scripts\\windows\\bootstrap-reference-library\.ps1/);
    assert.match(result.stdout, /fake pixi run wgsextract --config .*settings[\\/]config\.toml ref bootstrap --ref /);
  } finally {
    await processManager.terminateAllProcesses();
    setOrDeleteEnv("PIXI", previousPixi);
    setOrDeleteEnv("WGSEXTRACT_APP_DIR", previousAppDir);
    setOrDeleteEnv("WGSEXTRACT_REFERENCE_LIBRARY", previousReferenceLibrary);
    await rm(tempRoot, { force: true, recursive: true });
  }
});

test("WGSExtract Windows bootstrap runs ref bootstrap", async (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows PowerShell setup behavior is platform-specific.");
    return;
  }

  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const sourceScript = path.join(repoRoot, "examples", "WGSExtract", "scripts", "windows", "bootstrap-reference-library.ps1");
  const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-wgsextract-bootstrap-default-"));
  const scriptRoot = path.join(tempRoot, "scripts", "windows");
  const script = path.join(scriptRoot, "bootstrap-reference-library.ps1");
  const referenceLibrary = path.join(tempRoot, "reference");
  const previousReferenceLibrary = process.env.WGSEXTRACT_REFERENCE_LIBRARY;
  const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  try {
    await mkdir(scriptRoot, { recursive: true });
    await cp(sourceScript, script);
    await writeFile(path.join(scriptRoot, "run-wgsextract.ps1"), [
      "Write-Output \"fake wgsextract $($args -join ' ')\"",
      "exit 0",
      "",
    ].join("\r\n"));
    await mkdir(referenceLibrary, { recursive: true });
    process.env.WGSEXTRACT_REFERENCE_LIBRARY = referenceLibrary;

    const result = await processManager.runProcess("powershell.exe", [
      "-NoProfile",
      "-NonInteractive",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      script,
    ], { env: process.env });

    assert.equal(result.exitCode, 0, result.stderr);
    assert.match(result.stdout, /fake wgsextract ref bootstrap --ref /);
  } finally {
    await processManager.terminateAllProcesses();
    setOrDeleteEnv("WGSEXTRACT_REFERENCE_LIBRARY", previousReferenceLibrary);
    await rm(tempRoot, { force: true, recursive: true });
  }
});

test("runs WGSExtract uninstall steps and removes bundle runtime", async (t) => {
  if (process.platform !== "win32") {
    t.skip("This regression covers the packaged Windows uninstall script path.");
    return;
  }

  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const sourceBundleRoot = path.join(repoRoot, "examples", "WGSExtract");
  const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-wgsextract-uninstall-"));
  const bundleRoot = path.join(tempRoot, "WGSExtract");
  const runtimeRoot = path.join(bundleRoot, "runtime", "wgsextract-cli");
  const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  try {
    await cp(sourceBundleRoot, bundleRoot, { recursive: true });
    await mkdir(path.join(runtimeRoot, "app"), { recursive: true });

    const { loadManifestFromRoot } = await import("../dist/web/src/server/bundle-loader.js");
    const manifest = await loadManifestFromRoot(bundleRoot);
    const result = await runUninstall(manifest, bundleRoot, processManager.runProcess);

    assert.equal(result.status, "ok");
    assert.equal(result.results[0].id, "cleanup-wgsextract-runtime");
    await assert.rejects(() => mkdir(path.join(runtimeRoot, "sentinel")), /ENOENT/);
  } finally {
    await processManager.terminateAllProcesses();
    await rm(tempRoot, { force: true, recursive: true });
  }
});

test("uninstall reverses install-manifest items (owned directories)", async (t) => {
  if (process.platform !== "win32") {
    t.skip("install-manifest reversal currently covered on Windows only.");
    return;
  }

  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const sourceBundleRoot = path.join(repoRoot, "examples", "WGSExtract");
  const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-wgsextract-manifest-"));
  const bundleRoot = path.join(tempRoot, "WGSExtract");
  const runtimeRoot = path.join(bundleRoot, "runtime", "wgsextract-cli");
  const ownedDir = path.join(tempRoot, "fake-pixi-install");
  // Use a synthetic PATH entry that should not appear in any real user PATH.
  const phantomPathEntry = path.join(tempRoot, "no-such-bin-dir-for-pixi");
  // msys2Install pointing at a non-existent root must safely no-op (no MSYS2 to actually purge here).
  const phantomMsys2Root = path.join(tempRoot, "no-such-msys64-root");
  const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });

  try {
    await cp(sourceBundleRoot, bundleRoot, { recursive: true });
    await mkdir(path.join(runtimeRoot, "app"), { recursive: true });
    await mkdir(path.join(ownedDir, "bin"), { recursive: true });
    await writeFile(path.join(ownedDir, "bin", "fake.exe"), "");
    const manifestContents = {
      format: 1,
      createdAt: new Date().toISOString(),
      items: [
        { type: "directory", path: ownedDir, ownedBy: "install_windows.bat" },
        // PATH entry that is not present in the real User PATH: must no-op safely.
        { type: "userPathEntry", path: phantomPathEntry, ownedBy: "install_windows.bat" },
        // msys2Install at a non-existent root: must no-op safely (not error).
        { type: "msys2Install", root: phantomMsys2Root, ownedBy: "install_windows.bat" },
        // Unknown type: must be tolerated with a warning, not fail the run.
        { type: "futureType", path: "ignored", ownedBy: "future" },
      ],
    };
    await writeFile(path.join(runtimeRoot, "install-manifest.json"), JSON.stringify(manifestContents));

    const { loadManifestFromRoot } = await import("../dist/web/src/server/bundle-loader.js");
    const manifest = await loadManifestFromRoot(bundleRoot);
    const result = await runUninstall(manifest, bundleRoot, processManager.runProcess);

    assert.equal(result.status, "ok");
    assert.equal(result.results[0].id, "cleanup-wgsextract-runtime");
    await assert.rejects(() => mkdir(path.join(ownedDir, "sentinel")), /ENOENT/);
    await assert.rejects(() => mkdir(path.join(runtimeRoot, "sentinel")), /ENOENT/);
  } finally {
    await processManager.terminateAllProcesses();
    await rm(tempRoot, { force: true, recursive: true });
  }
});

test("Windows WGSExtract setup delegates to upstream install_windows.bat", async (t) => {
  // Source-level invariants: this test is platform-agnostic because it just reads scripts.
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const setupScript = path.join(repoRoot, "examples", "WGSExtract", "scripts", "windows", "setup-wgsextract-pixi.ps1");
  const uninstallScript = path.join(repoRoot, "examples", "WGSExtract", "scripts", "windows", "uninstall-wgsextract.ps1");

  const setupSource = await readFile(setupScript, "utf8");
  const uninstallSource = await readFile(uninstallScript, "utf8");

  // Setup must shell out to upstream's install_windows.bat (not run `pixi install` itself).
  assert.match(setupSource, /install_windows\.bat/);
  assert.doesNotMatch(setupSource, /(^|\s)& \$pixi install(\s|$)/m,
    "setup should delegate pixi install to install_windows.bat, not run it directly");

  // Setup must snapshot pre-state so the manifest knows whether to own MSYS2 / Pixi.
  assert.match(setupSource, /\$msys2PreInstalled\s*=\s*Test-MsysInstall/);
  assert.match(setupSource, /\$pixiPreInstalled\s*=/);

  // Setup must emit an msys2Install manifest item only when an MSYS2-based CLI
  // (0.6.x and older) newly installed MSYS2; newer CLIs take every tool from Pixi.
  assert.match(setupSource, /\$usesMsys2\s*=\s*Test-Path[\s\S]*?setup_pacman_runtime\.ps1/);
  assert.match(setupSource, /if \(\$usesMsys2 -and -not \$msys2PreInstalled\)[\s\S]*?type\s*=\s*"msys2Install"/);
  // Only those CLIs get MSYS2_ROOT and the user folder for it, and only their
  // install is checked for MSYS2 afterwards: native ones would fail that check.
  assert.match(setupSource, /if \(\$usesMsys2\) \{[\s\S]*?New-WGSExtractUserFolder[\s\S]*?\$env:MSYS2_ROOT = \$msys2Root[\s\S]*?\} else \{\s*\$msys2InUserFolder = \$false/);
  assert.equal(setupSource.match(/New-WGSExtractUserFolder/g)?.length, 1);
  assert.equal(setupSource.match(/\$env:MSYS2_ROOT\s*=/g)?.length, 1);
  assert.match(setupSource, /if \(\$usesMsys2 -and -not \(Test-MsysInstall -Root \$msys2Root\)\)/);
  assert.doesNotMatch(setupSource, /if \(-not \(Test-MsysInstall -Root \$msys2Root\)\)/);

  // Uninstall must dispatch on the msys2Install item type via a dedicated handler.
  assert.match(uninstallSource, /"msys2Install"\s*\{[\s\S]*?Invoke-Msys2Uninstall/);
  assert.match(uninstallSource, /Invoke-Msys2Uninstall[\s\S]*?uninstall\.exe[\s\S]*?purge[\s\S]*?--confirm-command[\s\S]*?--accept-messages/);

  // Safety check: refuse to remove if the root isn't a recognizable MSYS2 install.
  assert.match(uninstallSource, /Test-MsysRootSafeToRemove/);
});

function setOrDeleteEnv(key, value) {
  if (value === undefined) {
    delete process.env[key];
  } else {
    process.env[key] = value;
  }
}

function escapeRegExp(value) {
  return String(value).replace(/[\\^$.*+?()[\]{}|]/g, "\\$&");
}

function tomlBasicStringValue(value) {
  return String(value).replaceAll("\\", "\\\\").replaceAll('"', '\\"');
}

test("rejects unknown setup step ids", async () => {
  await assert.rejects(
    runSetupStep({ setup: { steps: [] } }, path.resolve("bundle"), async () => ({ exitCode: 0 }), "missing"),
    /Unknown setup step: missing/
  );
});

test("runs and persists initial setup when no prior setup run exists", async () => {
  const savedStates = [];
  const emittedEvents = [];
  const bundle = {
    manifest: {
      setup: {
        steps: [{ id: "pixi", kind: "pathTool", label: "Find Pixi", value: "pixi" }],
      },
    },
    bundleState: { setupRun: null },
  };
  const runProcess = async (_executable, _args, options) => {
    options.onStdout?.("pixi\n");
    return { exitCode: 0, stdout: "pixi\n", stderr: "" };
  };

  const setupRun = await runInitialSetupIfNeeded(
    bundle,
    path.resolve("bundle"),
    runProcess,
    async (state) => savedStates.push(state),
    (event) => emittedEvents.push(event),
    true,
    () => "2026-05-09T19:20:00.000Z",
  );

  assert.equal(setupRun.status, "ok");
  assert.equal(setupRun.completedAt, "2026-05-09T19:20:00.000Z");
  assert.equal(setupRun.results[0].id, "pixi");
  assert.equal(bundle.bundleState.setupRun, setupRun);
  assert.deepEqual(savedStates, [{ setupRun }]);
  assert.deepEqual(emittedEvents.map((event) => event.type), ["step-start", "output", "step-complete", "complete"]);
});

test("streams setup process output before step completion", async () => {
  const emittedEvents = [];
  const manifest = {
    setup: {
      steps: [{ id: "install", kind: "setupScript", label: "Install", value: "scripts/install.sh" }],
    },
  };
  const runProcess = async (_executable, _args, options) => {
    options.onStdout?.("downloading\n");
    options.onStderr?.("installing\n");
    return { exitCode: 0, stdout: "downloading\n", stderr: "installing\n" };
  };

  const setupRun = await runSetup(manifest, path.resolve("bundle"), runProcess, (event) => {
    emittedEvents.push(event);
  });

  assert.equal(setupRun.status, "ok");
  assert.deepEqual(emittedEvents.map((event) => event.type), [
    "step-start",
    "output",
    "output",
    "step-complete",
    "complete",
  ]);
  assert.equal(emittedEvents[1].text, "downloading\n");
  assert.equal(emittedEvents[2].text, "installing\n");
});

test("setup fails before running steps when initial install size exceeds free space", async () => {
  const emittedEvents = [];
  let setupStepRunCount = 0;
  const manifest = {
    setup: {
      initialInstallSizeGB: 1_000_000_000,
      steps: [{ id: "install", kind: "setupScript", label: "Install", value: "scripts/install.sh" }],
    },
  };
  const runProcess = async (executable) => {
    if (String(executable).endsWith("install.sh")) {
      setupStepRunCount += 1;
    }
    return { exitCode: 0, stdout: "", stderr: "" };
  };

  const setupRun = await runSetup(manifest, path.resolve("bundle"), runProcess, (event) => {
    emittedEvents.push(event);
  });

  assert.equal(setupStepRunCount, 0);
  assert.equal(setupRun.status, "failed");
  assert.match(setupRun.error, /Need .* GB free/);
  assert.deepEqual(emittedEvents.map((event) => event.type), ["complete"]);
  assert.equal(emittedEvents[0].result.preflight.severity, "warning");
});

test("streams setup process output before failed step completion", async () => {
  const emittedEvents = [];
  const manifest = {
    setup: {
      steps: [{ id: "install", kind: "setupScript", label: "Install", value: "scripts/install.sh" }],
    },
  };
  const runProcess = async (_executable, _args, options) => {
    options.onStdout?.("downloaded\n");
    options.onStderr?.("failed install\n");
    return { exitCode: 7, stdout: "downloaded\n", stderr: "failed install\n" };
  };

  const setupRun = await runSetup(manifest, path.resolve("bundle"), runProcess, (event) => {
    emittedEvents.push(event);
  });

  assert.equal(setupRun.status, "failed");
  assert.deepEqual(emittedEvents.map((event) => event.type), [
    "step-start",
    "output",
    "output",
    "step-complete",
    "complete",
  ]);
  assert.equal(emittedEvents[1].text, "downloaded\n");
  assert.equal(emittedEvents[2].text, "failed install\n");
  assert.equal(emittedEvents[3].result.status, "failed");
});

test("skips initial setup when disabled, already run, or no steps exist", async () => {
  let runCount = 0;
  const runProcess = async () => {
    runCount += 1;
    return { exitCode: 0 };
  };
  const saveState = async () => {
    throw new Error("setup should not be persisted");
  };

  assert.equal(
    await runInitialSetupIfNeeded({ manifest: { setup: { steps: [{ id: "a" }] } }, bundleState: {} }, path.resolve("bundle"), runProcess, saveState, undefined, false),
    null,
  );
  assert.equal(
    await runInitialSetupIfNeeded({ manifest: { setup: { steps: [{ id: "a" }] } }, bundleState: { setupRun: { status: "ok" } } }, path.resolve("bundle"), runProcess, saveState),
    null,
  );
  assert.equal(
    await runInitialSetupIfNeeded({ manifest: { setup: { steps: [] } }, bundleState: {} }, path.resolve("bundle"), runProcess, saveState),
    null,
  );
  assert.equal(runCount, 0);
});

test("persists a failed initial setup when launching a setup command throws", async () => {
  const savedStates = [];
  const bundle = {
    manifest: {
      setup: {
        steps: [{ id: "missing", kind: "pathTool", label: "Missing Tool", value: "missing-tool" }],
      },
    },
    bundleState: {},
  };

  const setupRun = await runInitialSetupIfNeeded(
    bundle,
    path.resolve("bundle"),
    async () => {
      throw new Error("spawn failed");
    },
    async (state) => savedStates.push(state),
    undefined,
    true,
    () => "2026-05-09T19:21:00.000Z",
  );

  assert.equal(setupRun.status, "failed");
  assert.equal(setupRun.error, "spawn failed");
  assert.equal(setupRun.completedAt, "2026-05-09T19:21:00.000Z");
  assert.deepEqual(setupRun.results, []);
  assert.deepEqual(savedStates, [{ setupRun }]);
});
