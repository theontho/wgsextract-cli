import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const cli = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../dist/web/src/server/bundle-uninstall-cli.js");
const { saveConfig } = await import("../dist/web/src/server/config-store.js");
const { normalizeContext } = await import("../dist/web/src/server/paths.js");
const { prepareBundleWorkspace } = await import("../dist/web/src/server/workspace.js");
const { appDataWorkspacesDirectory, appSettingsPath, defaultWorkspaceRoot, saveWorkspacesDirectory, workspaceLocationInfo } =
  await import("../dist/web/src/server/workspace-location.js");
const { moveWorkspace } = await import("../dist/web/src/server/workspace-move.js");
const { displayWorkspacePath, relocatePath } = await import("../dist/shared/workspace-paths.js");

const settingsControl = {
  id: "paths",
  label: "Paths",
  kind: "configEditor",
  configFile: { path: "{{bundleWorkspace}}/settings/config.toml", format: "toml" },
  settings: [
    { id: "output", key: "output_directory", label: "Output", kind: "path" },
    { id: "elsewhere", key: "elsewhere", label: "Elsewhere", kind: "path" },
  ],
};
const manifest = {
  id: "location.bundle",
  displayName: "Location Fixture",
  summary: "Tests where workspaces live.",
  pages: [{ id: "main", title: "Main", summary: "Main page.", sections: [{ id: "main", controls: [settingsControl] }] }],
};

function dataEnvironment(root) {
  return {
    HOME: root,
    USERPROFILE: root,
    XDG_DATA_HOME: path.join(root, "data"),
    LOCALAPPDATA: path.join(root, "data"),
    GUI_FOR_CLI_APP_SUPPORT_NAME: "test.app",
    GUI_FOR_CLI_BUNDLE_WORKSPACES_DIR: undefined,
  };
}

async function fixture(t, fixtureManifest = manifest) {
  const root = await mkdtemp(path.join(tmpdir(), "gui-for-cli-workspace-location-"));
  const original = {};
  for (const [key, value] of Object.entries(dataEnvironment(root))) {
    original[key] = process.env[key];
    if (value === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = value;
    }
  }
  t.after(async () => {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
    await rm(root, { force: true, recursive: true });
  });
  const source = path.join(root, "source");
  await mkdir(source, { recursive: true });
  await writeFile(path.join(source, "manifest.json"), JSON.stringify(fixtureManifest));
  return { root, source };
}

function move(request) {
  return moveWorkspace({ manifest, onProgress: () => undefined, signal: new AbortController().signal, ...request });
}

test("a bundle can default its workspace to a visible folder in the home folder", async (t) => {
  const declared = { ...manifest, defaultWorkspace: "~/visible-workspace" };
  const { source } = await fixture(t, declared);

  assert.equal(await prepareBundleWorkspace(declared, source), path.join(homedir(), "visible-workspace"));
  assert.equal(defaultWorkspaceRoot(manifest), path.join(appDataWorkspacesDirectory(), "location.bundle"));
  for (const defaultWorkspace of ["/etc/app", "~/.hidden", "~/a/b", "~/../up"]) {
    assert.throws(() => defaultWorkspaceRoot({ ...manifest, defaultWorkspace }), /defaultWorkspace/);
  }
});

test("a default workspace never takes over someone's folder of that name", async (t) => {
  const declared = { ...manifest, defaultWorkspace: "~/visible-workspace" };
  const { root, source } = await fixture(t, declared);
  await mkdir(path.join(root, "visible-workspace"));
  await writeFile(path.join(root, "visible-workspace", "manifest.json"), "someone else's");

  await assert.rejects(prepareBundleWorkspace(declared, source), /not a bundle workspace/);
  assert.equal(await readFile(path.join(root, "visible-workspace", "manifest.json"), "utf8"), "someone else's");
});

test("moving takes the contents along, and saved paths follow unless set elsewhere", async (t) => {
  const { root, source } = await fixture(t);
  const current = await prepareBundleWorkspace(manifest, source);
  const elsewhere = path.join(root, "my outputs");
  await saveConfig(settingsControl, undefined, { output_directory: "{{bundleWorkspace}}/output", elsewhere }, current);
  await mkdir(path.join(current, "reference"));
  await writeFile(path.join(current, "reference", "hg38.fa"), "ACGT");
  await writeFile(path.join(current, "state.json"), JSON.stringify({
    fieldValues: { bam: path.join(current, "genomes", "a.bam"), other: elsewhere },
    setupRun: { status: "ok" },
  }));

  const chosen = path.join(root, "big drive");
  await move({ sourceRoot: source, workspaceRoot: current, directory: chosen });

  const moved = path.join(chosen, "location.bundle");
  await assert.rejects(stat(current), { code: "ENOENT" });
  assert.equal(await readFile(path.join(moved, "reference", "hg38.fa"), "utf8"), "ACGT");
  const config = await readFile(path.join(moved, "settings", "config.toml"), "utf8");

  const { parseFlatToml } = await import("../dist/shared/rendering.js");
  assert.deepEqual({ ...parseFlatToml(config) }, { output_directory: path.join(moved, "output"), elsewhere });
  const state = JSON.parse(await readFile(path.join(moved, "state.json"), "utf8"));
  assert.deepEqual(state.fieldValues, { bam: path.join(moved, "genomes", "a.bam"), other: elsewhere });
  assert.equal(state.setupRun, null, "setup has to run again");

  assert.equal(await prepareBundleWorkspace(manifest, source), moved);
  const info = await workspaceLocationInfo(manifest, moved);
  assert.equal(info.configuredDirectory, chosen);
  assert.equal(info.usingDefault, false);

  await move({ sourceRoot: source, workspaceRoot: moved, directory: null });
  assert.equal(await prepareBundleWorkspace(manifest, source), current);
  assert.equal(await readFile(path.join(current, "reference", "hg38.fa"), "utf8"), "ACGT");
  assert.equal((await workspaceLocationInfo(manifest, current)).usingDefault, true);
});

test("moving leaves the CLI runtime behind, except its record of what setup installed", async (t) => {
  const { root, source } = await fixture(t);
  const current = await prepareBundleWorkspace(manifest, source);
  const tool = path.join(current, "runtime", "wgsextract-cli");
  await mkdir(path.join(tool, "app", ".pixi"), { recursive: true });
  await writeFile(path.join(tool, "app", ".pixi", "env"), "absolute paths");
  await writeFile(path.join(tool, "install-manifest.json"), "{}");

  await move({ sourceRoot: source, workspaceRoot: current, directory: path.join(root, "chosen") });

  const movedTool = path.join(root, "chosen", "location.bundle", "runtime", "wgsextract-cli");
  await assert.rejects(stat(path.join(movedTool, "app")), { code: "ENOENT" });
  assert.equal(await readFile(path.join(movedTool, "install-manifest.json"), "utf8"), "{}");
});

test("folders that are unsafe to move to are refused and nothing changes", async (t) => {
  const { root, source } = await fixture(t);
  const current = await prepareBundleWorkspace(manifest, source);
  const occupied = path.join(root, "occupied");
  await mkdir(path.join(occupied, "location.bundle"), { recursive: true });
  await writeFile(path.join(occupied, "location.bundle", "keep.txt"), "someone else's");

  for (const [directory, message] of [
    ["relative/path", /absolute/],
    [path.join(source, "inside"), /cannot be inside/],
    [path.join(current, "inside"), /cannot be inside/],
    [occupied, /already has files/],
  ]) {
    await assert.rejects(move({ sourceRoot: source, workspaceRoot: current, directory }), message);
  }

  await stat(path.join(current, "manifest.json"));
  await assert.rejects(stat(appSettingsPath()), { code: "ENOENT" });
  assert.equal(await readFile(path.join(occupied, "location.bundle", "keep.txt"), "utf8"), "someone else's");
});

test("the environment variable overrides the saved folder, and rules out moving", async (t) => {
  const { root, source } = await fixture(t);
  await saveWorkspacesDirectory(path.join(root, "saved"));
  process.env.GUI_FOR_CLI_BUNDLE_WORKSPACES_DIR = path.join(root, "from-env");

  const workspace = await prepareBundleWorkspace(manifest, source);

  assert.equal(workspace, path.join(root, "from-env", "location.bundle"));
  assert.equal((await workspaceLocationInfo(manifest, workspace)).environmentOverride, true);
  await assert.rejects(move({ sourceRoot: source, workspaceRoot: workspace, directory: path.join(root, "chosen") }), /environment variable/);
});

test("a chosen folder keeps its workspace across a changed bundle source", async (t) => {
  const { root, source } = await fixture(t);
  await saveWorkspacesDirectory(path.join(root, "chosen"));
  const workspace = await prepareBundleWorkspace(manifest, source);
  await writeFile(path.join(workspace, "state.json"), "{}\n");

  const updatedSource = path.join(root, "updated-source");
  await mkdir(updatedSource);
  await writeFile(path.join(updatedSource, "manifest.json"), JSON.stringify(manifest));

  assert.equal(await prepareBundleWorkspace(manifest, updatedSource), workspace);
  assert.equal(await readFile(path.join(workspace, "state.json"), "utf8"), "{}\n");
});

test("paths in the workspace are shown with the token, and real paths reach the settings file and commands", async (t) => {
  const { root } = await fixture(t);
  const workspace = path.join(root, "workspace");
  const output = path.join(workspace, "output");

  assert.equal(displayWorkspacePath(output, workspace), "{{bundleWorkspace}}/output");
  assert.equal(displayWorkspacePath(workspace, workspace), "{{bundleWorkspace}}");
  assert.equal(displayWorkspacePath(`${workspace}-other`, workspace), `${workspace}-other`);
  assert.equal(displayWorkspacePath("C:\\Users\\Me\\WGS-Extract\\output", "c:\\users\\me\\wgs-extract"), "{{bundleWorkspace}}/output");
  assert.equal(relocatePath("/home/me/w/a/b", "/home/me/w", "D:\\w"), "D:\\w\\a\\b");

  const { path: configFile } = await saveConfig(settingsControl, undefined, { output_directory: "{{bundleWorkspace}}/output", elsewhere: "" }, workspace);
  assert.match(await readFile(configFile, "utf8"), /output_directory = /);
  assert.doesNotMatch(await readFile(configFile, "utf8"), /bundleWorkspace/);

  const context = normalizeContext({ fieldValues: { out_dir: "{{bundleWorkspace}}/output", threads: 4 } }, workspace);
  assert.deepEqual(context.fieldValues, { out_dir: output, threads: 4 });
});

function runUninstallCli(source, ...flags) {
  return new Promise((resolve) => {
    execFile(process.execPath, [cli, "--app-support-name", "test.app", ...flags, source], { env: process.env },
      (error, stdout, stderr) => resolve({ code: error ? error.code : 0, stdout, stderr }));
  });
}

test("uninstall keeps the workspace unless told to remove it, and always removes the old default location", async (t) => {
  const { root, source } = await fixture(t);
  const chosen = path.join(root, "chosen");
  const oldWorkspace = await prepareBundleWorkspace(manifest, source);
  await saveWorkspacesDirectory(chosen);
  const workspace = await prepareBundleWorkspace(manifest, source);
  await writeFile(path.join(chosen, "keep.txt"), "not the app's");

  const printed = await runUninstallCli(source, "--print-workspace");
  assert.equal(printed.stdout.trim(), workspace, printed.stderr);
  await stat(oldWorkspace);

  const kept = await runUninstallCli(source);
  assert.equal(kept.code, 0, `${kept.stdout}\n${kept.stderr}`);
  await stat(path.join(workspace, "manifest.json"));
  await assert.rejects(stat(oldWorkspace), { code: "ENOENT" });
  await stat(appSettingsPath());

  const removed = await runUninstallCli(source, "--remove-workspace", "--remove-app-data");
  assert.equal(removed.code, 0, `${removed.stdout}\n${removed.stderr}`);
  await assert.rejects(stat(workspace), { code: "ENOENT" });
  await assert.rejects(stat(path.dirname(appSettingsPath())), { code: "ENOENT" });
  assert.equal(await readFile(path.join(chosen, "keep.txt"), "utf8"), "not the app's");
  assert.equal((await runUninstallCli(source, "--print-workspace")).stdout, "");
});
