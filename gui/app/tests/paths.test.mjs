import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

const { expandPathTokens, parseArgs } = await import("../dist/web/src/server/paths.js");

test("path token expansion preserves unset environment variables", () => {
  const key = "GUI_FOR_CLI_MISSING_TEST_PATH_TOKEN";
  const previous = process.env[key];
  try {
    delete process.env[key];
    assert.equal(
      expandPathTokens(`\${${key}}/output`, "/bundle"),
      `\${${key}}/output`,
    );
  } finally {
    if (previous === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = previous;
    }
  }
});

test("path token expansion resolves set environment variables", () => {
  const key = "GUI_FOR_CLI_SET_TEST_PATH_TOKEN";
  const previous = process.env[key];
  try {
    process.env[key] = "/resolved";
    assert.equal(
      expandPathTokens(`\${${key}}/output`, "/bundle"),
      "/resolved/output",
    );
  } finally {
    if (previous === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = previous;
    }
  }
});

test("path token expansion exposes bundle workspace file URLs", () => {
  const workspace = path.resolve("tmp", "bundle workspace");

  assert.equal(
    expandPathTokens("{{bundleWorkspaceFileURL}}/fixture.txt", workspace),
    `${pathToFileURL(workspace).href}/fixture.txt`,
  );
});

test("path token expansion normalizes token path separators on Windows", (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows path separator behavior is platform-specific.");
    return;
  }

  assert.equal(
    expandPathTokens("{{bundleWorkspace}}/reference/genomes/hg19.fa.gz", "C:\\bundle\\workspace"),
    path.join("C:\\bundle\\workspace", "reference", "genomes", "hg19.fa.gz"),
  );
});

test("Windows app data lives under LOCALAPPDATA, not a POSIX-style ~/.local/share", { skip: process.platform !== "win32" && "Windows-only" }, async () => {
  const { applicationSupportDirectory } = await import("../dist/web/src/server/paths.js");
  const previous = process.env.LOCALAPPDATA;
  process.env.LOCALAPPDATA = "C:\\Users\\me\\AppData\\Local";
  try {
    assert.equal(applicationSupportDirectory(), "C:\\Users\\me\\AppData\\Local");
  } finally {
    process.env.LOCALAPPDATA = previous;
  }
});

test("--port rejects values that are not a port from 0 to 65535 (#527)", () => {
  for (const value of ["banana", "-1", "65536", "80.5", "1e3", " 80", "+80", "0x50"]) {
    assert.throws(() => parseArgs(["--port", value]), /--port.*0 to 65535/, `--port ${JSON.stringify(value)}`);
  }
});

test("--port accepts the boundary ports 0 and 65535 (#527)", () => {
  assert.equal(parseArgs(["--port", "0"]).port, "0");
  assert.equal(parseArgs(["--port", "65535"]).port, "65535");
  assert.equal(parseArgs(["--bundle", "b"]).port, undefined);
  assert.throws(() => parseArgs(["--port"]), /Missing value for --port/);
});
