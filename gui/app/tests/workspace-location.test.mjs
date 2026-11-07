import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const cli = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../dist/web/src/server/bundle-uninstall-cli.js");
const { prepareBundleWorkspace } = await import("../dist/web/src/server/workspace.js");
const { appSettingsPath, defaultWorkspacesDirectory, saveWorkspacesDirectory, workspaceLocationInfo } =
  await import("../dist/web/src/server/workspace-location.js");

const manifest = {
  id: "location.bundle",
  displayName: "Location Fixture",
  summary: "Tests where workspaces live.",
  pages: [{ id: "main", title: "Main", summary: "Main page.", sections: [{ id: "main" }] }],
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

async function fixture(t) {
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
  await writeFile(path.join(source, "manifest.json"), JSON.stringify(manifest));
  return { root, source };
}

test("workspaces default to the app's data folder until a folder is chosen", async (t) => {
  const { root, source } = await fixture(t);
  const current = await prepareBundleWorkspace(manifest, source);
  assert.equal(current, path.join(defaultWorkspacesDirectory(), "location.bundle"));

  const chosen = path.join(root, "big drive", "workspaces");
  await saveWorkspacesDirectory(chosen, manifest.id, source, current);

  assert.deepEqual(await workspaceLocationInfo(manifest.id, current), {
    workspaceRoot: current,
    defaultDirectory: defaultWorkspacesDirectory(),
    configuredDirectory: chosen,
    environmentOverride: false,
    restartRequired: true,
  });
  const moved = await prepareBundleWorkspace(manifest, source);
  assert.equal(moved, path.join(chosen, "location.bundle"));
  assert.equal((await workspaceLocationInfo(manifest.id, moved)).restartRequired, false);
  // The old workspace is left where it was.
  await stat(path.join(current, "manifest.json"));

  await saveWorkspacesDirectory(null, manifest.id, source, moved);
  assert.equal(await prepareBundleWorkspace(manifest, source), current);
});

test("the environment variable overrides the saved folder", async (t) => {
  const { root, source } = await fixture(t);
  await saveWorkspacesDirectory(path.join(root, "saved"), manifest.id, source, path.join(root, "current"));
  process.env.GUI_FOR_CLI_BUNDLE_WORKSPACES_DIR = path.join(root, "from-env");

  const workspace = await prepareBundleWorkspace(manifest, source);

  assert.equal(workspace, path.join(root, "from-env", "location.bundle"));
  assert.equal((await workspaceLocationInfo(manifest.id, workspace)).environmentOverride, true);
});

test("a chosen folder keeps its workspace across a changed bundle source", async (t) => {
  const { root, source } = await fixture(t);
  const chosen = path.join(root, "chosen");
  await saveWorkspacesDirectory(chosen, manifest.id, source, path.join(root, "current"));
  const workspace = await prepareBundleWorkspace(manifest, source);
  await writeFile(path.join(workspace, "state.json"), "{}\n");

  const updatedSource = path.join(root, "updated-source");
  await mkdir(updatedSource);
  await writeFile(path.join(updatedSource, "manifest.json"), JSON.stringify(manifest));

  assert.equal(await prepareBundleWorkspace(manifest, updatedSource), workspace);
  assert.equal(await readFile(path.join(workspace, "state.json"), "utf8"), "{}\n");
});

test("folders that are unsafe to use are refused and nothing is saved", async (t) => {
  const { root, source } = await fixture(t);
  const current = path.join(root, "current");
  const stranger = path.join(root, "stranger");
  await mkdir(path.join(stranger, "location.bundle"), { recursive: true });
  await writeFile(path.join(stranger, "location.bundle", "manifest.json"), "someone else's");
  const refusals = [
    ["relative/path", /absolute/],
    [path.join(source, "inside"), /cannot be inside/],
    [path.join(current, "inside"), /cannot be inside/],
    [stranger, /not a bundle workspace/],
  ];

  for (const [directory, message] of refusals) {
    await assert.rejects(saveWorkspacesDirectory(directory, manifest.id, source, current), message);
  }

  await assert.rejects(stat(appSettingsPath()), { code: "ENOENT" });
  assert.equal(await readFile(path.join(stranger, "location.bundle", "manifest.json"), "utf8"), "someone else's");
});

test("a saved folder that became unmanaged stops startup rather than overwriting it", async (t) => {
  const { root, source } = await fixture(t);
  const chosen = path.join(root, "chosen");
  await saveWorkspacesDirectory(chosen, manifest.id, source, path.join(root, "current"));
  await mkdir(path.join(chosen, "location.bundle"), { recursive: true });
  await writeFile(path.join(chosen, "location.bundle", "manifest.json"), "someone else's");

  await assert.rejects(prepareBundleWorkspace(manifest, source), /not a bundle workspace/);
  assert.equal(await readFile(path.join(chosen, "location.bundle", "manifest.json"), "utf8"), "someone else's");
});

test("uninstall removes a workspace kept outside the app's data folder but not its siblings", async (t) => {
  const { root, source } = await fixture(t);
  const chosen = path.join(root, "chosen");
  await saveWorkspacesDirectory(chosen, manifest.id, source, path.join(root, "current"));
  const workspace = await prepareBundleWorkspace(manifest, source);
  await writeFile(path.join(chosen, "keep.txt"), "not the app's");
  const appData = path.dirname(appSettingsPath());

  const { code, stdout, stderr } = await new Promise((resolve) => {
    execFile(process.execPath, [cli, "--app-support-name", "test.app", "--remove-app-data", source], { env: process.env },
      (error, out, err) => resolve({ code: error ? error.code : 0, stdout: out, stderr: err }));
  });

  assert.equal(code, 0, `${stdout}\n${stderr}`);
  await assert.rejects(stat(workspace), { code: "ENOENT" });
  await assert.rejects(stat(appData), { code: "ENOENT" });
  assert.equal(await readFile(path.join(chosen, "keep.txt"), "utf8"), "not the app's");
});
