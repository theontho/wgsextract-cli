import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, readdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { crc32, deflateRawSync, gzipSync } from "node:zlib";

const { bundleArchiveFormat, extractBundleArchive, safeEntryPath } = await import("../dist/web/src/server/bundle-archive.js");
const { resolveBundleSourceRoot } = await import("../dist/web/src/server/bundle-loader.js");

const MANIFEST = JSON.stringify({ id: "archived", displayName: "Archived", summary: "From an archive." });
const SCRIPT = "#!/bin/sh\necho ok\n";

function tarHeader(name, size, type = "0", mode = 0o644) {
  const header = Buffer.alloc(512);
  header.write(name, 0, 100, "utf8");
  header.write(`${mode.toString(8).padStart(7, "0")}\0`, 100);
  header.write(`${size.toString(8).padStart(11, "0")}\0`, 124);
  header.write(type, 156);
  header.write("ustar\0", 257);
  header.fill(" ", 148, 156);
  let sum = 0;
  for (const byte of header) sum += byte;
  header.write(`${sum.toString(8).padStart(6, "0")}\0 `, 148);
  return header;
}

function tarArchive(files) {
  const blocks = [];
  for (const [name, content, mode] of files) {
    const data = Buffer.from(content);
    blocks.push(tarHeader(name, data.length, "0", mode), data, Buffer.alloc((512 - (data.length % 512)) % 512));
  }
  blocks.push(Buffer.alloc(1024));
  return Buffer.concat(blocks);
}

function zipArchive(files) {
  const locals = [];
  const centrals = [];
  let offset = 0;
  for (const [index, [name, content, mode]] of files.entries()) {
    const data = Buffer.from(content);
    const deflate = index % 2 === 0;
    const stored = deflate ? deflateRawSync(data) : data;
    const nameBytes = Buffer.from(name);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(deflate ? 8 : 0, 8);
    local.writeUInt32LE(crc32(data), 14);
    local.writeUInt32LE(stored.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(0x031e, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(deflate ? 8 : 0, 10);
    central.writeUInt32LE(crc32(data), 16);
    central.writeUInt32LE(stored.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBytes.length, 28);
    central.writeUInt32LE(((mode ?? 0o644) << 16) >>> 0, 38);
    central.writeUInt32LE(offset, 42);
    locals.push(local, nameBytes, stored);
    centrals.push(central, nameBytes);
    offset += local.length + nameBytes.length + stored.length;
  }
  const centralDirectory = Buffer.concat(centrals);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(files.length, 8);
  end.writeUInt16LE(files.length, 10);
  end.writeUInt32LE(centralDirectory.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, centralDirectory, end]);
}

async function withTempDir(run) {
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-archive-"));
  try {
    await run(directory);
  }
  finally {
    await rm(directory, { recursive: true, force: true });
  }
}

const BUNDLE_FILES = [
  ["bundle/manifest.json", MANIFEST],
  ["bundle/scripts/run.sh", SCRIPT, 0o755],
];

test("recognizes bundle archive formats by extension", () => {
  assert.equal(bundleArchiveFormat("b.ZIP"), "zip");
  assert.equal(bundleArchiveFormat("b.tar"), "tar");
  assert.equal(bundleArchiveFormat("b.tar.gz"), "tgz");
  assert.equal(bundleArchiveFormat("b.tgz"), "tgz");
  assert.equal(bundleArchiveFormat("manifest.json.gz"), "gzipManifest");
  assert.equal(bundleArchiveFormat("b.txt"), null);
});

for (const [format, build] of [
  ["zip", (files) => zipArchive(files)],
  ["tar", (files) => tarArchive(files)],
  ["tgz", (files) => gzipSync(tarArchive(files))],
]) {
  test(`extracts a ${format} bundle with a single top-level folder`, async () => {
    await withTempDir(async (directory) => {
      const archive = path.join(directory, `bundle.${format}`);
      await writeFile(archive, build(BUNDLE_FILES));

      const root = await extractBundleArchive(archive, format, path.join(directory, "cache"));

      assert.equal(path.basename(root), "bundle");
      assert.equal(await readFile(path.join(root, "manifest.json"), "utf8"), MANIFEST);
      assert.equal(await readFile(path.join(root, "scripts", "run.sh"), "utf8"), SCRIPT);
      if (process.platform !== "win32") {
        assert.equal((await stat(path.join(root, "scripts", "run.sh"))).mode & 0o111, 0o111);
      }
    });
  });
}

test("extracts a gzipped manifest and reuses the cached extraction", async () => {
  await withTempDir(async (directory) => {
    const archive = path.join(directory, "manifest.json.gz");
    await writeFile(archive, gzipSync(MANIFEST));
    const cache = path.join(directory, "cache");

    const root = await extractBundleArchive(archive, "gzipManifest", cache);
    await writeFile(path.join(root, "marker"), "kept");
    const again = await extractBundleArchive(archive, "gzipManifest", cache);

    assert.equal(again, root);
    assert.equal(await readFile(path.join(again, "marker"), "utf8"), "kept");
  });
});

test("rejects archive entries that escape the bundle", async () => {
  assert.throws(() => safeEntryPath("/dest", "../evil.sh"), /escapes the bundle/);
  assert.throws(() => safeEntryPath("/dest", "/etc/passwd"), /escapes the bundle/);
  assert.throws(() => safeEntryPath("/dest", "C:\\evil.exe"), /escapes the bundle/);
  await withTempDir(async (directory) => {
    const archive = path.join(directory, "evil.zip");
    await writeFile(archive, zipArchive([["manifest.json", MANIFEST], ["../evil.sh", SCRIPT]]));
    await assert.rejects(() => extractBundleArchive(archive, "zip", path.join(directory, "cache")), /escapes the bundle/);
  });
});

test("ignores macOS metadata beside the bundle folder", async () => {
  await withTempDir(async (directory) => {
    const archive = path.join(directory, "bundle.zip");
    await writeFile(archive, zipArchive([...BUNDLE_FILES, ["__MACOSX/bundle/._manifest.json", "meta"]]));

    const root = await extractBundleArchive(archive, "zip", path.join(directory, "cache"));

    assert.equal(path.basename(root), "bundle");
  });
});

test("rejects truncated tar archives", async () => {
  await withTempDir(async (directory) => {
    const archive = path.join(directory, "cut.tar");
    const whole = tarArchive([...BUNDLE_FILES, ["bundle/data.bin", "x".repeat(2000)]]);
    await writeFile(archive, whole.subarray(0, whole.length - 1024 - 1024));
    const cache = path.join(directory, "cache");
    await assert.rejects(() => extractBundleArchive(archive, "tar", cache), /truncated/);
    assert.deepEqual(await readdir(cache), []);
  });
});

test("rejects archives without a manifest", async () => {
  await withTempDir(async (directory) => {
    const archive = path.join(directory, "empty.tar");
    await writeFile(archive, tarArchive([["a/readme.txt", "hi"], ["b/readme.txt", "hi"]]));
    await assert.rejects(() => extractBundleArchive(archive, "tar", path.join(directory, "cache")), /no manifest\.json/);
  });
});

test("bundle source resolver extracts an archive made by the system tar", async (t) => {
  try {
    execFileSync("tar", ["--version"], { stdio: "ignore" });
  }
  catch {
    t.skip("tar is not installed");
    return;
  }
  await withTempDir(async (directory) => {
    await mkdir(path.join(directory, "src", "my-bundle", "scripts"), { recursive: true });
    await writeFile(path.join(directory, "src", "my-bundle", "manifest.json"), MANIFEST);
    await writeFile(path.join(directory, "src", "my-bundle", "scripts", "run.sh"), SCRIPT);
    const archive = path.join(directory, "my-bundle.tgz");
    // Relative paths: MSYS tar, first on PATH in Git hook shells, reads C:\... as a remote host.
    execFileSync("tar", ["-czf", path.basename(archive), "-C", "src", "my-bundle"], { cwd: directory });

    const root = await resolveBundleSourceRoot(archive);

    assert.equal(path.basename(root), "my-bundle");
    assert.equal(await readFile(path.join(root, "manifest.json"), "utf8"), MANIFEST);
  });
});
