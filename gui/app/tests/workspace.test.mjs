import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, stat, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

test("bundle workspace sync keeps runtime, state, config and data, and drops what the bundle dropped", async () => {
  await withTemporaryHome("gui-for-cli-webui-workspace-", async (tempRoot) => {
    const originalAppSupportName = process.env.GUI_FOR_CLI_APP_SUPPORT_NAME;
    process.env.GUI_FOR_CLI_APP_SUPPORT_NAME = "dev.guiforcli.webui";

    try {
      const { prepareBundleWorkspace } = await import("../dist/web/src/server/workspace.js");
      const { appSupportDirectory } = await import("../dist/web/src/server/paths.js");
      const sourceRoot = path.join(tempRoot, "source");
      await mkdir(path.join(sourceRoot, "old-scripts"), { recursive: true });
      await writeFile(path.join(sourceRoot, "manifest.json"), "{\"id\":\"stateful.bundle\"}\n");
      await writeFile(path.join(sourceRoot, "stale.txt"), "dropped by the next bundle version");
      await writeFile(path.join(sourceRoot, "old-scripts", "run.sh"), "dropped too");

      const manifest = {
        id: "stateful.bundle",
        pages: [
          {
            sections: [
              {
                controls: [
                  {
                    id: "settings",
                    kind: "configEditor",
                    configFile: { path: "{{bundleWorkspace}}/settings/config.toml" },
                  },
                ],
              },
            ],
          },
        ],
      };

      const workspaceRoot = await prepareBundleWorkspace(manifest, sourceRoot);
      assert.equal(
        workspaceRoot,
        path.join(appSupportDirectory(), "BundleWorkspaces", "stateful.bundle"),
      );
      await mkdir(path.join(workspaceRoot, "runtime"), { recursive: true });
      await writeFile(path.join(workspaceRoot, "runtime", "installed.txt"), "keep runtime");
      await writeFile(path.join(workspaceRoot, "state.json"), "{\"colorTheme\":\"dark\"}\n");
      await mkdir(path.join(workspaceRoot, "settings"), { recursive: true });
      await writeFile(path.join(workspaceRoot, "settings", "config.toml"), "output_directory = \"/tmp/out\"\n");
      // What the bundle's commands keep in its workspace: a reference library, a user's outputs.
      await mkdir(path.join(workspaceRoot, "reference"), { recursive: true });
      await writeFile(path.join(workspaceRoot, "reference", "genome.fa"), "keep reference");
      await writeFile(path.join(workspaceRoot, "notes.txt"), "keep notes");
      // A new bundle version without stale.txt and old-scripts/.
      await rm(path.join(sourceRoot, "stale.txt"));
      await rm(path.join(sourceRoot, "old-scripts"), { recursive: true });

      await prepareBundleWorkspace(manifest, sourceRoot);

      assert.equal(await readFile(path.join(workspaceRoot, "runtime", "installed.txt"), "utf8"), "keep runtime");
      assert.equal(await readFile(path.join(workspaceRoot, "state.json"), "utf8"), "{\"colorTheme\":\"dark\"}\n");
      assert.equal(
        await readFile(path.join(workspaceRoot, "settings", "config.toml"), "utf8"),
        "output_directory = \"/tmp/out\"\n",
      );
      await assert.rejects(stat(path.join(workspaceRoot, "stale.txt")), /ENOENT/);
      await assert.rejects(stat(path.join(workspaceRoot, "old-scripts")), /ENOENT/);
      assert.equal(await readFile(path.join(workspaceRoot, "reference", "genome.fa"), "utf8"), "keep reference");
      assert.equal(await readFile(path.join(workspaceRoot, "notes.txt"), "utf8"), "keep notes");
    } finally {
      if (originalAppSupportName == null) {
        delete process.env.GUI_FOR_CLI_APP_SUPPORT_NAME;
      } else {
        process.env.GUI_FOR_CLI_APP_SUPPORT_NAME = originalAppSupportName;
      }
    }
  });
});

test("bundle workspace sync metadata resyncs changed source files", async () => {
  await withTemporaryHome("gui-for-cli-webui-workspace-sync-", async (tempRoot) => {
    const { prepareBundleWorkspace } = await import("../dist/web/src/server/workspace.js");
    const sourceRoot = path.join(tempRoot, "source");
    await mkdir(path.join(sourceRoot, "assets"), { recursive: true });
    await writeFile(path.join(sourceRoot, "manifest.json"), "{\"id\":\"sync.bundle\"}\n");
    await writeFile(path.join(sourceRoot, "assets", "message.txt"), "first\n");
    const manifest = { id: "sync.bundle", pages: [] };

    const workspaceRoot = await prepareBundleWorkspace(manifest, sourceRoot);
    const metadataPath = path.join(workspaceRoot, ".workspace-sync.json");
    const firstMetadata = await readFile(metadataPath, "utf8");
    assert.equal(await readFile(path.join(workspaceRoot, "assets", "message.txt"), "utf8"), "first\n");

    const messagePath = path.join(sourceRoot, "assets", "message.txt");
    const firstMessageMtime = (await stat(messagePath)).mtimeMs;
    await writeFile(messagePath, "second changed\n");
    await forceMtimeAdvance(messagePath, firstMessageMtime);
    await prepareBundleWorkspace(manifest, sourceRoot);

    assert.equal(await readFile(path.join(workspaceRoot, "assets", "message.txt"), "utf8"), "second changed\n");
    assert.notEqual(await readFile(metadataPath, "utf8"), firstMetadata);
  });
});

test("explicit bundle workspace refuses unmanaged non-empty directories", async () => {
  await withTemporaryHome("gui-for-cli-webui-explicit-workspace-", async (tempRoot) => {
    const { prepareBundleWorkspace } = await import("../dist/web/src/server/workspace.js");
    const sourceRoot = path.join(tempRoot, "source");
    const workspaceRoot = path.join(tempRoot, "not-a-workspace");
    await mkdir(sourceRoot, { recursive: true });
    await mkdir(workspaceRoot, { recursive: true });
    await writeFile(path.join(sourceRoot, "manifest.json"), "{\"id\":\"explicit.bundle\"}\n");
    await writeFile(path.join(workspaceRoot, "keep.txt"), "do not delete\n");

    await assert.rejects(
      prepareBundleWorkspace({ id: "explicit.bundle", pages: [] }, sourceRoot, workspaceRoot),
      /Refusing to use non-empty unmanaged workspace root/,
    );
    assert.equal(await readFile(path.join(workspaceRoot, "keep.txt"), "utf8"), "do not delete\n");
  });
});

test("explicit bundle workspace refuses spoofed managed markers", async () => {
  await withTemporaryHome("gui-for-cli-webui-explicit-workspace-spoofed-", async (tempRoot) => {
    const { prepareBundleWorkspace } = await import("../dist/web/src/server/workspace.js");
    const sourceRoot = path.join(tempRoot, "source");
    const workspaceRoot = path.join(tempRoot, "not-a-workspace");
    await mkdir(sourceRoot, { recursive: true });
    await mkdir(workspaceRoot, { recursive: true });
    await writeFile(path.join(sourceRoot, "manifest.json"), "{\"id\":\"explicit.bundle\"}\n");
    await writeFile(path.join(workspaceRoot, ".bundle-workspace"), "GUI for CLI bundle workspace\n");
    await writeFile(path.join(workspaceRoot, ".workspace-sync.json"), JSON.stringify({
      version: 1,
      manifestID: "other.bundle",
      sourceRoot,
      preservedNames: [],
      entries: [],
    }));
    await writeFile(path.join(workspaceRoot, "keep.txt"), "do not delete\n");

    await assert.rejects(
      prepareBundleWorkspace({ id: "explicit.bundle", pages: [] }, sourceRoot, workspaceRoot),
      /Refusing to use non-empty unmanaged workspace root/,
    );
    assert.equal(await readFile(path.join(workspaceRoot, "keep.txt"), "utf8"), "do not delete\n");
  });
});

test("explicit bundle workspace allows empty directories and writes a sentinel", async () => {
  await withTemporaryHome("gui-for-cli-webui-explicit-workspace-empty-", async (tempRoot) => {
    const { prepareBundleWorkspace } = await import("../dist/web/src/server/workspace.js");
    const sourceRoot = path.join(tempRoot, "source");
    const workspaceRoot = path.join(tempRoot, "workspace");
    await mkdir(sourceRoot, { recursive: true });
    await mkdir(workspaceRoot, { recursive: true });
    await writeFile(path.join(sourceRoot, "manifest.json"), "{\"id\":\"explicit.bundle\"}\n");

    const result = await prepareBundleWorkspace({ id: "explicit.bundle", pages: [] }, sourceRoot, workspaceRoot);

    assert.equal(result, workspaceRoot);
    assert.equal(await readFile(path.join(workspaceRoot, ".bundle-workspace"), "utf8"), "GUI for CLI bundle workspace\n");
  });
});

test("bundle workspace sync marks nested scripts executable", async (t) => {
  if (process.platform === "win32") {
    t.skip("POSIX executable bits are platform-specific.");
    return;
  }

  await withTemporaryHome("gui-for-cli-webui-workspace-scripts-", async (tempRoot) => {
    const { prepareBundleWorkspace } = await import("../dist/web/src/server/workspace.js");
    const sourceRoot = path.join(tempRoot, "source");
    await mkdir(path.join(sourceRoot, "scripts", "posix"), { recursive: true });
    await writeFile(path.join(sourceRoot, "manifest.json"), "{\"id\":\"script.bundle\"}\n");
    await writeFile(path.join(sourceRoot, "scripts", "posix", "test-genome-library.py"), "#!/usr/bin/env python3\n");
    await writeFile(path.join(sourceRoot, "scripts", "posix", "extensionless-helper"), "#!/bin/sh\n");
    await writeFile(path.join(sourceRoot, "scripts", "posix", "LICENSE"), "not a script\n");
    await writeFile(path.join(sourceRoot, "scripts", "posix", "data.json"), "{}\n");

    const workspaceRoot = await prepareBundleWorkspace({ id: "script.bundle", pages: [] }, sourceRoot);
    const scriptMode = (await stat(path.join(workspaceRoot, "scripts", "posix", "test-genome-library.py"))).mode;
    const extensionlessMode = (await stat(path.join(workspaceRoot, "scripts", "posix", "extensionless-helper"))).mode;
    const licenseMode = (await stat(path.join(workspaceRoot, "scripts", "posix", "LICENSE"))).mode;
    const dataMode = (await stat(path.join(workspaceRoot, "scripts", "posix", "data.json"))).mode;

    assert.notEqual(scriptMode & 0o111, 0);
    assert.notEqual(extensionlessMode & 0o111, 0);
    assert.equal(licenseMode & 0o111, 0);
    assert.equal(dataMode & 0o111, 0);
  });
});

test("bundle workspace sync ignores non-directory scripts entries", async (t) => {
  if (process.platform === "win32") {
    t.skip("POSIX executable bits are platform-specific.");
    return;
  }

  await withTemporaryHome("gui-for-cli-webui-workspace-scripts-file-", async (tempRoot) => {
    const { prepareBundleWorkspace } = await import("../dist/web/src/server/workspace.js");
    const sourceRoot = path.join(tempRoot, "source");
    await mkdir(sourceRoot, { recursive: true });
    await writeFile(path.join(sourceRoot, "manifest.json"), "{\"id\":\"script-file.bundle\"}\n");
    await writeFile(path.join(sourceRoot, "scripts"), "not a directory\n");

    const workspaceRoot = await prepareBundleWorkspace({ id: "script-file.bundle", pages: [] }, sourceRoot);
    const scriptsMode = (await stat(path.join(workspaceRoot, "scripts"))).mode;

    assert.equal(await readFile(path.join(workspaceRoot, "scripts"), "utf8"), "not a directory\n");
    assert.equal(scriptsMode & 0o111, 0);
  });
});

test("bundle workspace sync ignores nested hidden files when fingerprinting", async () => {
  await withTemporaryHome("gui-for-cli-webui-workspace-hidden-", async (tempRoot) => {
    const { prepareBundleWorkspace } = await import("../dist/web/src/server/workspace.js");
    const sourceRoot = path.join(tempRoot, "source");
    await mkdir(path.join(sourceRoot, "assets"), { recursive: true });
    await writeFile(path.join(sourceRoot, "manifest.json"), "{\"id\":\"hidden.bundle\"}\n");
    await writeFile(path.join(sourceRoot, "assets", "message.txt"), "visible\n");
    await writeFile(path.join(sourceRoot, "assets", ".ignored.txt"), "hidden\n");
    const manifest = { id: "hidden.bundle", pages: [] };

    const workspaceRoot = await prepareBundleWorkspace(manifest, sourceRoot);
    const metadataPath = path.join(workspaceRoot, ".workspace-sync.json");
    const firstMetadata = await readFile(metadataPath, "utf8");
    assert.equal(await readFile(path.join(workspaceRoot, "assets", ".ignored.txt"), "utf8"), "hidden\n");

    const ignoredPath = path.join(sourceRoot, "assets", ".ignored.txt");
    const firstIgnoredMtime = (await stat(ignoredPath)).mtimeMs;
    await writeFile(ignoredPath, "hidden changed\n");
    await forceMtimeAdvance(ignoredPath, firstIgnoredMtime);
    await prepareBundleWorkspace(manifest, sourceRoot);

    assert.equal(await readFile(metadataPath, "utf8"), firstMetadata);
    assert.equal(await readFile(path.join(workspaceRoot, "assets", ".ignored.txt"), "utf8"), "hidden\n");
  });
});

async function forceMtimeAdvance(filePath, previousMtimeMs) {
  const nextMtime = new Date(Math.max(Date.now(), Math.ceil(previousMtimeMs) + 2_000));
  await utimes(filePath, nextMtime, nextMtime);
}

async function withTemporaryHome(prefix, callback) {
  const tempRoot = await mkdtemp(path.join(tmpdir(), prefix));
  const originalHome = process.env.HOME;
  const originalUserProfile = process.env.USERPROFILE;
  const originalXdgDataHome = process.env.XDG_DATA_HOME;

  process.env.HOME = tempRoot;
  process.env.USERPROFILE = tempRoot;
  process.env.XDG_DATA_HOME = path.join(tempRoot, ".local", "share");

  try {
    return await callback(tempRoot);
  } finally {
    restoreEnv("HOME", originalHome);
    restoreEnv("USERPROFILE", originalUserProfile);
    restoreEnv("XDG_DATA_HOME", originalXdgDataHome);
    await rm(tempRoot, { recursive: true, force: true });
  }
}

function restoreEnv(name, value) {
  if (value == null) {
    delete process.env[name];
  } else {
    process.env[name] = value;
  }
}

test("bundle state persists selected page id", async () => {
  const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-webui-state-"));

  try {
    const { loadBundleState, saveBundleState } = await import("../dist/web/src/server/config-store.js");
    await saveBundleState(
      {
        localizationCode: "en",
        selectedPageID: "library",
        setupRun: {
          status: "ok",
          results: [{ id: "pixi", label: "Pixi", kind: "pathTool", status: "ok", exitCode: 0 }],
          completedAt: "2026-05-09T18:54:22Z",
        },
        iconSet: "emoji",
        colorTheme: "dark",
        webUIFont: "sfPro",
      },
      tempRoot,
    );

    const state = await loadBundleState(tempRoot);
    assert.equal(state.selectedPageID, "library");
    assert.equal(state.setupRun.status, "ok");
    assert.equal(state.setupRun.results[0].id, "pixi");
    assert.equal(state.localizationCode, "en");
    assert.equal(state.iconSet, "emoji");
    assert.equal(state.colorTheme, "dark");
    assert.equal(state.webUIFont, "sfPro");
  } finally {
    await rm(tempRoot, { recursive: true, force: true });
  }
});
