import assert from "node:assert/strict";
import { chmod, lstat, mkdir, mkdtemp, readdir, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { parseFlatToml } from "../dist/shared/rendering.js";

const { bootstrapConfigFiles, loadBundleState, loadConfig, saveBundleState, saveConfig, takeBundleStateWarning, writeFileAtomically } = await import("../dist/web/src/server/config-store.js");
const { loadLocalizedBundle } = await import("../dist/web/src/server/bundle-loader.js");

async function tempDirectory(t) {
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-config-store-"));
  t.after(() => rm(directory, { force: true, recursive: true }));
  return directory;
}

test("a truncated state.json is backed up and reported instead of silently reset", async (t) => {
  const bundleRoot = await tempDirectory(t);
  await writeFile(path.join(bundleRoot, "state.json"), '{"selectedPageID": "vcf", "fieldValues": {"sam');

  const state = await loadBundleState(bundleRoot);

  assert.equal(state.selectedPageID, null);
  const backups = (await readdir(bundleRoot)).filter((name) => name.startsWith("state.json.corrupt-"));
  assert.equal(backups.length, 1);
  assert.equal(await readFile(path.join(bundleRoot, backups[0]), "utf8"), '{"selectedPageID": "vcf", "fieldValues": {"sam');
  assert.match(takeBundleStateWarning(bundleRoot), /could not be read.*state\.json\.corrupt-/);
  assert.equal(takeBundleStateWarning(bundleRoot), undefined);
});

test("state.json read errors other than a missing file are not swallowed", async (t) => {
  const bundleRoot = await tempDirectory(t);
  await mkdir(path.join(bundleRoot, "state.json"));

  await assert.rejects(loadBundleState(bundleRoot), /EISDIR/);
});

test("state saves go through a temp file and leave no partial files behind", async (t) => {
  const bundleRoot = await tempDirectory(t);

  await Promise.all([
    saveBundleState({ selectedPageID: "a" }, bundleRoot),
    saveBundleState({ fieldValues: { sample: "HG001" } }, bundleRoot),
  ]);

  const saved = JSON.parse(await readFile(path.join(bundleRoot, "state.json"), "utf8"));
  assert.equal(saved.selectedPageID, "a");
  assert.deepEqual(saved.fieldValues, { sample: "HG001" });
  assert.deepEqual(await readdir(bundleRoot), ["state.json"]);
});

test("atomic writes follow symlinks and keep file permissions", { skip: process.platform === "win32" }, async (t) => {
  const directory = await tempDirectory(t);
  const target = path.join(directory, "real.toml");
  const link = path.join(directory, "linked.toml");
  await writeFile(target, "old\n");
  await chmod(target, 0o600);
  await symlink(target, link);

  await writeFileAtomically(link, "new\n");

  assert.equal(await readFile(target, "utf8"), "new\n");
  assert.equal((await stat(target)).mode & 0o777, 0o600);
  assert.ok((await lstat(link)).isSymbolicLink());
});

function parseSavedToml(text) {
  for (const python of ["python3", "python"]) {
    const result = spawnSync(python, [
      "-c", "import json,sys,tomllib; print(json.dumps(tomllib.loads(sys.stdin.read())))",
    ], { input: text, encoding: "utf8" });
    if (result.error?.code === "ENOENT" || /No module named ['"]tomllib['"]/.test(result.stderr ?? "")) continue;
    assert.equal(result.status, 0, result.stderr || String(result.error ?? ""));
    return JSON.parse(result.stdout);
  }
  return parseFlatToml(text);
}

test("saving WGSExtract Settings keeps disabled/enabled/disabled toggles as native TOML booleans", async (t) => {
  const bundleRoot = await tempDirectory(t);
  const page = JSON.parse(await readFile(new URL("../../examples/WGSExtract/pages/settings.json", import.meta.url), "utf8"));
  const control = page.sections.flatMap((section) => section.controls ?? []).find((item) => item.kind === "configEditor");

  for (const [value, expected] of [[false, false], ["true", true], ["false", false]]) {
    const saved = await saveConfig(control, undefined, {
      atomic_outputs: value, keep_awake: value, output_directory: "123", reference_library: "false",
    }, bundleRoot);
    const text = await readFile(saved.path, "utf8");
    const parsed = parseSavedToml(text);
    assert.equal(parsed.atomic_outputs, expected);
    assert.equal(parsed.keep_awake, expected);
    assert.equal(parsed.output_directory, "123");
    assert.equal(parsed.reference_library, "false");
    assert.match(text, new RegExp(`atomic_outputs = ${expected}\\n`));
    const loaded = await loadConfig(control, undefined, bundleRoot);
    assert.equal(loaded.values.atomic_outputs, expected);
    assert.equal(loaded.values.keep_awake, expected);
  }
});

test("typed setting defaults preserve numbers and booleans when bootstrap merges missing keys", async (t) => {
  const bundleRoot = await tempDirectory(t);
  const control = {
    id: "settings", kind: "configEditor",
    configFile: { path: "{{bundleWorkspace}}/settings/typed.toml", bootstrap: { mode: "mergeMissing" } },
    settings: [
      { id: "enabled", key: "enabled", kind: "toggle", value: false },
      { id: "workers", key: "workers", kind: "text", value: 4 },
      { id: "ratio", key: "ratio", kind: "text", value: 0.5 },
      { id: "label", key: "label", kind: "text", value: "default" },
    ],
  };
  const saved = await saveConfig(control, undefined, {
    enabled: "false", workers: "8", ratio: "1.5", label: "false",
  }, bundleRoot);
  control.settings.push({ id: "added", key: "added", kind: "text", value: "new" });
  await bootstrapConfigFiles({ pages: [{ sections: [{ controls: [control] }] }] }, bundleRoot, { settings: saved.path });
  assert.deepEqual({ ...parseSavedToml(await readFile(saved.path, "utf8")) }, {
    added: "new", enabled: false, label: "false", ratio: 1.5, workers: 8,
  });

  await writeFile(saved.path, 'enabled = false\nworkers = 1_000\nratio = 1_0.5_0\nlabel = "1_000"\n');
  const loaded = await loadConfig(control, undefined, bundleRoot);
  assert.equal(loaded.values.workers, 1000);
  assert.equal(loaded.values.ratio, 10.5);
  assert.equal(loaded.values.label, "1_000");
  await bootstrapConfigFiles({ pages: [{ sections: [{ controls: [control] }] }] }, bundleRoot, { settings: saved.path });
  assert.deepEqual({ ...parseSavedToml(await readFile(saved.path, "utf8")) }, {
    added: "new", enabled: false, label: "1_000", ratio: 10.5, workers: 1000,
  });
  for (const literal of ["0x8", "0o10", "0b1000", "0x0_8", "0o1_0", "0b10_00"]) {
    await writeFile(saved.path, `enabled = false\nworkers = ${literal}\nratio = 1.5\nlabel = "${literal}"\n`);
    const based = await loadConfig(control, undefined, bundleRoot);
    assert.equal(based.values.workers, 8);
    assert.equal(based.values.label, literal);
    await bootstrapConfigFiles({ pages: [{ sections: [{ controls: [control] }] }] }, bundleRoot, { settings: saved.path });
    const published = parseSavedToml(await readFile(saved.path, "utf8"));
    assert.equal(published.workers, 8);
    assert.equal(published.label, literal);
  }
  await assert.rejects(saveConfig(control, undefined, { workers: "invalid" }, bundleRoot), /Invalid number value/);
});

test("invalid saved scalars warn and use defaults without blocking bundle load or repairing the file silently", async (t) => {
  const bundleRoot = await tempDirectory(t);
  const control = {
    id: "settings", label: "Settings", kind: "configEditor",
    configFile: { path: "settings.toml", format: "toml" },
    settings: [
      { id: "atomic_outputs", key: "atomic_outputs", label: "Atomic", kind: "toggle", value: false },
      { id: "workers", key: "workers", label: "Workers", kind: "text", value: 4 },
      { id: "label", key: "label", label: "Label", kind: "text", value: "default" },
    ],
  };
  await writeFile(path.join(bundleRoot, "manifest.json"), JSON.stringify({
    id: "invalid-config", displayName: "Invalid config", summary: "Test saved values",
    pages: [{ id: "settings", title: "Settings", summary: "Test Settings", sections: [{ id: "settings", controls: [control] }] }],
  }));
  const invalidToml = 'atomic_outputs = 1\nworkers = "invalid"\nlabel = true\n';
  await writeFile(path.join(bundleRoot, "settings.toml"), invalidToml);
  const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
  const bundle = await loadLocalizedBundle("en", repoRoot, bundleRoot, bundleRoot);
  assert.equal(bundle.configValues["settings.atomic_outputs"], false);
  assert.equal(bundle.configValues["settings.workers"], 4);
  assert.equal(bundle.configValues["settings.label"], "default");
  assert.match(bundle.configWarnings.join("\n"), /Invalid boolean.*atomic_outputs.*declared default/);
  assert.match(bundle.configWarnings.join("\n"), /Invalid number.*workers.*declared default/);
  assert.match(bundle.configWarnings.join("\n"), /Invalid string.*label.*declared default/);
  const loaded = await loadConfig(control, undefined, bundleRoot);
  assert.deepEqual(loaded.values, { atomic_outputs: false, workers: 4, label: "default" });
  assert.equal(loaded.warnings.length, 3);
  assert.equal(await readFile(loaded.path, "utf8"), invalidToml);
  await assert.rejects(saveConfig(control, undefined, { atomic_outputs: 1 }, bundleRoot), /Invalid boolean/);
  await writeFile(loaded.path, 'atomic_outputs = "true"\nworkers = "8"\nlabel = "label"\n');
  const quotedScalars = await loadConfig(control, undefined, bundleRoot);
  assert.deepEqual(quotedScalars.values, { atomic_outputs: false, workers: 4, label: "label" });
  assert.equal(quotedScalars.warnings.length, 2);
});
