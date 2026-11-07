import assert from "node:assert/strict";
import test from "node:test";

globalThis.localStorage = {
  getItem() {
    return null;
  },
  setItem() {},
};
globalThis.window = {
  innerHeight: 900,
  setTimeout,
  clearTimeout,
};

const { createInitialState, state } = await import("../dist/web/src/client/state.js");
const { checkForUpdates, downloadUpdate, initializeTauriUpdater, installUpdate } = await import("../dist/web/src/client/tauri-updater.js");

function resetState() {
  Object.assign(state, createInitialState(), {
    applicationVersion: "1.0.0",
  });
  delete globalThis.window.__TAURI__;
}

function setTauriMock(invoke) {
  globalThis.window.__TAURI__ = {
    core: { invoke },
    event: { listen: async () => () => {} },
  };
}

test("dedupes concurrent updater checks", async () => {
  resetState();
  let resolveCheck;
  let callCount = 0;
  setTauriMock((command, args) => {
    callCount += 1;
    assert.equal(command, "gfc_update_check");
    assert.deepEqual(args, { priorUpdateRid: null, priorBytesRid: null });
    return new Promise((resolve) => {
      resolveCheck = resolve;
    });
  });

  const first = checkForUpdates({ revealOnAvailable: true });
  const second = checkForUpdates({ revealOnAvailable: false });

  assert.equal(callCount, 1);
  assert.equal(typeof first?.then, "function");
  assert.equal(typeof second?.then, "function");

  resolveCheck({
    currentVersion: "1.0.0",
    availableVersion: "1.1.0",
    updateRid: 7,
    body: "Release notes",
  });
  await Promise.all([first, second]);

  assert.equal(state.update.status, "available");
  assert.equal(state.update.updateRid, 7);
  assert.equal(state.update.availableVersion, "1.1.0");
  assert.equal(state.update.popoverVisible, true);
});

test("passes prior updater resources to a replacement check", async () => {
  resetState();
  setTauriMock((command, args) => {
    assert.equal(command, "gfc_update_check");
    assert.deepEqual(args, { priorUpdateRid: 7, priorBytesRid: 9 });
    return Promise.resolve({
      currentVersion: "1.0.0",
      availableVersion: "1.1.0",
      updateRid: 11,
      body: "Release notes",
    });
  });
  state.update.updateRid = 7;
  state.update.bytesRid = 9;
  state.update.status = "downloaded";

  await checkForUpdates();

  assert.equal(state.update.status, "available");
  assert.equal(state.update.updateRid, 11);
  assert.equal(state.update.bytesRid, null);
});

test("skips re-download when update bytes are already cached", async () => {
  resetState();
  let invoked = false;
  setTauriMock(() => {
    invoked = true;
    throw new Error("download should not be invoked");
  });
  state.update.updateRid = 7;
  state.update.bytesRid = 9;
  state.update.status = "error";

  await downloadUpdate();

  assert.equal(invoked, false);
  assert.equal(state.update.status, "error");
  assert.equal(state.update.bytesRid, 9);
});

test("does not invoke install without both updater resources", async () => {
  resetState();
  let invoked = false;
  setTauriMock(() => {
    invoked = true;
    return Promise.resolve();
  });
  state.update.updateRid = 7;
  state.update.bytesRid = null;

  await installUpdate();

  assert.equal(invoked, false);
  assert.equal(state.update.status, "idle");
});

test("applies updater progress events from Tauri", async () => {
  resetState();
  let progressHandler;
  const originalSetTimeout = globalThis.window.setTimeout;
  globalThis.window.setTimeout = () => 0;
  globalThis.window.__TAURI__ = {
    core: {
      invoke: async () => {
        throw new Error("scheduled check should not run");
      },
    },
    event: {
      listen: async (event, handler) => {
        assert.equal(event, "gfc-update-progress");
        progressHandler = handler;
        return () => {};
      },
    },
  };

  try {
    initializeTauriUpdater();
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(state.update.supported, true);
    assert.equal(typeof progressHandler, "function");

    progressHandler({
      payload: {
        event: "progress",
        version: "1.1.0",
        downloadedBytes: 512,
        contentLength: 1024,
        percent: 50,
      },
    });

    assert.equal(state.update.availableVersion, "1.1.0");
    assert.equal(state.update.downloadedBytes, 512);
    assert.equal(state.update.contentLength, 1024);
    assert.equal(state.update.percent, 50);

    progressHandler({
      payload: {
        event: "finished",
        version: "1.1.0",
        downloadedBytes: 0,
        contentLength: null,
        percent: null,
      },
    });

    assert.equal(state.update.percent, 100);
    assert.equal(state.update.message, "Download complete. Ready to install.");
  } finally {
    globalThis.window.setTimeout = originalSetTimeout;
  }
});
