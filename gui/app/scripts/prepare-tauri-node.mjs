#!/usr/bin/env node
import { createHash } from "node:crypto";
import { createWriteStream } from "node:fs";
import { chmod, cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import https from "node:https";
import os from "node:os";
import path from "node:path";
import { pipeline } from "node:stream/promises";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";

const nodeVersion = "24.22.0";
// `--target <platform>-<arch>` (e.g. win32-x64) prepares another OS's runtime, for cross-building
// an installer; the default is the host's.
const [platform, arch] = targetPlatformArch(process.argv.slice(2));
const platformArch = `${platform}-${arch}`;
const nodePlatformArch = nodeDistributionPlatformArch(platform, arch);

if (!nodePlatformArch) {
  throw new Error(`Unsupported Tauri Node runtime platform: ${platformArch}`);
}

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const webuiRoot = path.resolve(scriptDir, "..");
const resourcesRoot = path.join(webuiRoot, "web", "packagers", "tauri", "resources", "node");
const cacheRoot = path.join(webuiRoot, ".cache", "tauri-node");
const nodeDistName = `node-v${nodeVersion}-${nodePlatformArch}`;
const archiveExtension =
  platform === "win32" ? "zip" : platform === "linux" ? "tar.xz" : "tar.gz";
const archiveName = `${nodeDistName}.${archiveExtension}`;
const archiveURL = `https://nodejs.org/dist/v${nodeVersion}/${archiveName}`;
const shasumsURL = `https://nodejs.org/dist/v${nodeVersion}/SHASUMS256.txt`;
const archivePath = path.join(cacheRoot, archiveName);
const extractRoot = path.join(cacheRoot, "extract");
const nodeExecutableRelativePath = platform === "win32" ? "node.exe" : path.join("bin", "node");
const nodeOutputPath = path.join(resourcesRoot, nodeExecutableRelativePath);
const versionPath = path.join(resourcesRoot, "VERSION");
const maxRedirects = 5;

await mkdir(cacheRoot, { recursive: true });
await mkdir(path.dirname(nodeOutputPath), { recursive: true });

const existingVersion = await readTextIfExists(versionPath);
if (existingVersion?.trim() === `v${nodeVersion} ${nodePlatformArch}`) {
  try {
    await chmod(nodeOutputPath, 0o755);
    console.log(`Tauri Node runtime already prepared: ${nodeOutputPath}`);
    process.exit(0);
  } catch {
    // Continue and refresh the runtime.
  }
}

console.log(`Preparing Tauri Node runtime ${nodeDistName}`);
if (!(await exists(archivePath))) {
  console.log(`Downloading ${archiveURL}`);
  await download(archiveURL, archivePath);
}

const expectedHash = await expectedSha256();
const actualHash = await sha256File(archivePath);
if (actualHash !== expectedHash) {
  await rm(archivePath, { force: true });
  throw new Error(`SHA256 mismatch for ${archiveName}: expected ${expectedHash}, got ${actualHash}`);
}

await rm(extractRoot, { recursive: true, force: true });
await mkdir(extractRoot, { recursive: true });
// GNU tar (Linux, or first on PATH in a Pixi or Homebrew environment) can't
// read the Windows .zip; Windows' own tar can, and unzip is everywhere else.
if (archiveExtension === "zip" && process.platform !== "win32") {
  await run("unzip", ["-q", archivePath, "-d", extractRoot]);
} else {
  await run("tar", ["-xf", archivePath, "-C", extractRoot]);
}
await rm(resourcesRoot, { recursive: true, force: true });
await mkdir(path.dirname(nodeOutputPath), { recursive: true });
await cp(path.join(extractRoot, nodeDistName, nodeExecutableRelativePath), nodeOutputPath);
await chmod(nodeOutputPath, 0o755);
await writeFile(versionPath, `v${nodeVersion} ${nodePlatformArch}\n`, "utf8");
console.log(`Prepared Tauri Node runtime: ${nodeOutputPath}`);

function targetPlatformArch(argv) {
  const index = argv.indexOf("--target");
  if (index < 0) {
    return [os.platform(), os.arch()];
  }
  const match = /^(darwin|linux|win32)-(arm64|x64)$/.exec(argv[index + 1] ?? "");
  if (!match) {
    throw new Error(`--target must be <darwin|linux|win32>-<arm64|x64>, got ${argv[index + 1] ?? "nothing"}`);
  }
  return [match[1], match[2]];
}

function nodeDistributionPlatformArch(osPlatform, osArch) {
  if (osPlatform === "darwin" && (osArch === "arm64" || osArch === "x64")) {
    return `darwin-${osArch}`;
  }
  if (osPlatform === "win32" && (osArch === "arm64" || osArch === "x64")) {
    return `win-${osArch}`;
  }
  if (osPlatform === "linux" && (osArch === "arm64" || osArch === "x64")) {
    return `linux-${osArch}`;
  }
  return undefined;
}

async function expectedSha256() {
  const shasumsPath = path.join(cacheRoot, `SHASUMS256-v${nodeVersion}.txt`);
  if (!(await exists(shasumsPath))) {
    console.log(`Downloading ${shasumsURL}`);
    await download(shasumsURL, shasumsPath);
  }
  const shasums = await readFile(shasumsPath, "utf8");
  for (const line of shasums.split(/\r?\n/)) {
    const [hash, file] = line.trim().split(/\s+/);
    if (file === archiveName) {
      return hash;
    }
  }
  throw new Error(`Could not find ${archiveName} in SHASUMS256.txt`);
}

function download(url, destination, redirectsLeft = maxRedirects) {
  return new Promise((resolve, reject) => {
    if (redirectsLeft <= 0) {
      reject(new Error(`Too many redirects for ${url}`));
      return;
    }
    https.get(url, (response) => {
      if (response.statusCode && response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
        response.resume();
        const redirectURL = new URL(response.headers.location, url).toString();
        download(redirectURL, destination, redirectsLeft - 1).then(resolve, reject);
        return;
      }
      if (response.statusCode !== 200) {
        reject(new Error(`Download failed (${response.statusCode}) for ${url}`));
        response.resume();
        return;
      }
      pipeline(response, createWriteStream(destination)).then(resolve, reject);
    }).on("error", reject);
  });
}

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: "inherit" });
    child.on("error", reject);
    child.on("exit", (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`${command} exited with ${code}`));
      }
    });
  });
}

async function exists(filePath) {
  try {
    await readFile(filePath);
    return true;
  } catch {
    return false;
  }
}

async function readTextIfExists(filePath) {
  try {
    return await readFile(filePath, "utf8");
  } catch {
    return undefined;
  }
}

async function sha256File(filePath) {
  const hash = createHash("sha256");
  hash.update(await readFile(filePath));
  return hash.digest("hex");
}
