import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { createServer } from "node:http";
import test from "node:test";

const { macOSPickerScript, pickPath, takeNativePickerEndpoint } = await import("../dist/web/src/server/path-picker.js");
const {
  bundlePickerDefaultPath,
  parentDirectory,
  rememberBundlePickerPath,
} = await import("../dist/web/src/client/bundle-picker-memory.js");
const { pathPickerDefaultPath } = await import("../dist/web/src/client/path-picker-defaults.js");

test("delegates path picking to the Tauri native picker bridge", async () => {
  const server = createServer((request, response) => {
    const url = new URL(request.url, "http://127.0.0.1");
    assert.equal(url.pathname, "/pick");
    assert.equal(url.searchParams.get("kind"), "directory");
    assert.equal(url.searchParams.get("title"), "Choose output");
    assert.equal(request.headers["x-gfc-token"], "picker-token");
    response.writeHead(200, { "content-type": "application/json; charset=utf-8" });
    response.end(JSON.stringify({ path: "C:\\Users\\mac\\Documents", kind: "directory", cancelled: false }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  try {
    assert.deepEqual(await pickPath({ kind: "directory", title: "Choose output", nativePicker: { port: address.port, token: "picker-token" } }), {
      path: "C:\\Users\\mac\\Documents",
      kind: "directory",
      cancelled: false,
    });
  } finally {
    await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  }
});

test("forwards bundle picks to the native picker and rejects unknown kinds", async () => {
  const server = createServer((request, response) => {
    assert.equal(new URL(request.url, "http://127.0.0.1").searchParams.get("kind"), "bundle");
    response.writeHead(200, { "content-type": "application/json; charset=utf-8" });
    response.end(JSON.stringify({ path: "/bundles/tool.zip", kind: "bundle", cancelled: false }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  try {
    const nativePicker = { port: address.port, token: "picker-token" };
    assert.deepEqual(await pickPath({ kind: "bundle", nativePicker }), { path: "/bundles/tool.zip", kind: "bundle", cancelled: false });
    await assert.rejects(() => pickPath({ kind: "archive", nativePicker }), /file, directory, or bundle/);
  } finally {
    await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  }
});

test("macOS picker AppleScript compiles", { skip: process.platform !== "darwin" && "osacompile is macOS-only" }, async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "gfc-picker-script-"));
  try {
    execFileSync("/usr/bin/osacompile", ["-o", path.join(directory, "picker.scpt"), "-e", macOSPickerScript]);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("preserves native picker cancellation", async () => {
  const server = createServer((_request, response) => {
    response.writeHead(200, { "content-type": "application/json; charset=utf-8" });
    response.end(JSON.stringify({ kind: "file", cancelled: true }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  try {
    assert.deepEqual(await pickPath({ kind: "file", nativePicker: { port: address.port, token: "picker-token" } }), { kind: "file", cancelled: true });
  } finally {
    await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  }
});

test("takes the native picker endpoint out of the environment so children never inherit it", () => {
  const environment = { GFC_NATIVE_PICKER_PORT: "4567", PATH: "/bin" };

  assert.deepEqual(takeNativePickerEndpoint("token", environment), { port: 4567, token: "token" });
  assert.deepEqual(environment, { PATH: "/bin" });
  assert.equal(takeNativePickerEndpoint("token", {}), undefined);
  assert.throws(() => takeNativePickerEndpoint("token", { GFC_NATIVE_PICKER_PORT: "nope" }), /Invalid GFC_NATIVE_PICKER_PORT/);
});

test("uses configured genome library as the default picker directory", () => {
  assert.equal(
    pathPickerDefaultPath(
      { id: "bam_path", kind: "path", defaultDirectory: "{{genome_library}}" },
      "",
      {
        fieldValues: { genome_library: "/genomes" },
        checkedOptions: {},
        configValues: {},
        bundleRootPath: "/bundle",
      },
    ),
    "/genomes",
  );
  assert.equal(
    pathPickerDefaultPath(
      { id: "bam_path", kind: "path", defaultDirectory: "{{genome_library}}" },
      "/samples/current.bam",
      {
        fieldValues: { genome_library: "/genomes" },
        checkedOptions: {},
        configValues: {},
        bundleRootPath: "/bundle",
      },
    ),
    "/samples/current.bam",
  );
});

test("bundle picker remembers its last directory separately from normal path defaults", () => {
  const storage = new MemoryStorage();
  storage.setItem("guiForCLI.pathPicker.lastDirectory", "/ordinary/files");

  assert.equal(bundlePickerDefaultPath("/initial/bundle", storage), "/initial/bundle");
  rememberBundlePickerPath("/examples/GitHelper", storage);

  assert.equal(bundlePickerDefaultPath("/initial/bundle", storage), "/examples");
  rememberBundlePickerPath("C:\\Bundles\\GitHelper\\manifest.json", storage);
  assert.equal(bundlePickerDefaultPath("", storage), "C:\\Bundles");
  rememberBundlePickerPath("/downloads/GitHelper.zip", storage);
  assert.equal(bundlePickerDefaultPath("", storage), "/downloads");
  assert.equal(storage.getItem("guiForCLI.pathPicker.lastDirectory"), "/ordinary/files");
  assert.equal(parentDirectory("C:\\Users\\mac\\Bundles\\CurlWorkbench"), "C:\\Users\\mac\\Bundles");
});

class MemoryStorage {
  constructor() {
    this.values = new Map();
  }

  getItem(key) {
    return this.values.has(key) ? this.values.get(key) : null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }
}
