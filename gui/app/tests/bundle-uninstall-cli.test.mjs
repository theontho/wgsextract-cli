import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { cp, mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const cli = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../dist/web/src/server/bundle-uninstall-cli.js");

async function fixture(t, script) {
  const root = await mkdtemp(path.join(tmpdir(), "gui-for-cli-bundle-uninstall-"));
  t.after(() => rm(root, { force: true, recursive: true }));
  const bundle = path.join(root, "bundle");
  await mkdir(path.join(bundle, "scripts"), { recursive: true });
  await writeFile(path.join(bundle, "scripts", "uninstall.sh"), script, { mode: 0o755 });
  await writeFile(path.join(bundle, "manifest.json"), JSON.stringify({
    id: "uninstall-fixture",
    displayName: "Uninstall Fixture",
    summary: "Tests uninstalling without the app.",
    uninstall: { steps: [{ id: "remove-runtime", kind: "setupScript", label: "Remove runtime", value: "scripts/uninstall.sh" }] },
    pages: [{ id: "main", title: "Main", summary: "Main page.", sections: [{ id: "main" }] }],
  }));
  // Where the app keeps the bundle's workspace for the app support name "test.app".
  const data = process.platform === "darwin" ? path.join(root, "Library", "Application Support") : path.join(root, "data");
  const workspace = path.join(data, "test.app", "BundleWorkspaces", "uninstall-fixture");
  return { root, bundle, workspace, result: path.join(root, "result.json") };
}

function runCli(paths, ...flags) {
  return new Promise((resolve) => {
    execFile(process.execPath, [cli, "--app-support-name", "test.app", ...flags, paths.bundle], {
      env: { ...process.env, HOME: paths.root, XDG_DATA_HOME: path.join(paths.root, "data"), GUI_FOR_CLI_UNINSTALL_RESULT_FILE: paths.result },
    }, (error, stdout, stderr) => resolve({ code: error ? error.code : 0, stdout, stderr }));
  });
}

test("runs the bundle's uninstall steps on the app's workspace and reports the result", { skip: process.platform === "win32" }, async (t) => {
  const paths = await fixture(t, '#!/bin/sh\necho "removing runtime"\nrm -rf "$GUI_FOR_CLI_BUNDLE_WORKSPACE/runtime"\n');
  await cp(paths.bundle, paths.workspace, { recursive: true });
  await mkdir(path.join(paths.workspace, "runtime"));

  const { code, stdout } = await runCli(paths);

  assert.equal(code, 0, stdout);
  assert.match(stdout, /removing runtime/);
  await assert.rejects(readFile(path.join(paths.workspace, "runtime")), { code: "ENOENT" });
  const result = JSON.parse(await readFile(paths.result, "utf8"));
  assert.equal(result.status, "ok");
  assert.deepEqual(result.results.map((step) => [step.id, step.status]), [["remove-runtime", "ok"]]);
});

test("a failed uninstall step fails the command and its result", { skip: process.platform === "win32" }, async (t) => {
  const paths = await fixture(t, "#!/bin/sh\necho 'could not remove' >&2\nexit 3\n");
  await cp(paths.bundle, paths.workspace, { recursive: true });

  const { code } = await runCli(paths);

  assert.equal(code, 1);
  assert.equal(JSON.parse(await readFile(paths.result, "utf8")).status, "failed");
});

test("with no workspace (setup never ran) there is nothing to remove", { skip: process.platform === "win32" }, async (t) => {
  const paths = await fixture(t, "#!/bin/sh\nexit 3\n");

  const { code, stdout } = await runCli(paths);

  assert.equal(code, 0);
  assert.match(stdout, /nothing to remove/);
  const result = JSON.parse(await readFile(paths.result, "utf8"));
  assert.equal(result.status, "ok");
  assert.deepEqual(result.results, []);
});

test("--remove-app-data deletes the app's data after the steps, and keeps it if one failed", { skip: process.platform === "win32" }, async (t) => {
  const removed = await fixture(t, "#!/bin/sh\nexit 0\n");
  await cp(removed.bundle, removed.workspace, { recursive: true });
  const appData = path.dirname(path.dirname(removed.workspace));
  await mkdir(path.join(appData, "WebKitCache"), { recursive: true });

  assert.equal((await runCli(removed, "--remove-app-data")).code, 0);
  await assert.rejects(stat(appData), { code: "ENOENT" });
  assert.deepEqual(JSON.parse(await readFile(removed.result, "utf8")).log, [`Removed ${appData}`]);

  const kept = await fixture(t, "#!/bin/sh\nexit 3\n");
  await cp(kept.bundle, kept.workspace, { recursive: true });
  assert.equal((await runCli(kept, "--remove-app-data")).code, 1);
  await stat(path.join(kept.workspace, "manifest.json"));
});
