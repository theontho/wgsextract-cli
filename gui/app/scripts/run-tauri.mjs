#!/usr/bin/env node
import { existsSync } from "node:fs";
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawn, execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { loadBundleMetadata } from "./bundle-metadata.mjs";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const execFileAsync = promisify(execFile);

const webuiRoot = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(webuiRoot, "..");
const devConfigPath = path.join(repoRoot, ".devconfig.toml");
const tauriDir = path.join(webuiRoot, "web", "packagers", "tauri");
const tauriScript = path.join(
  webuiRoot,
  "node_modules",
  "@tauri-apps",
  "cli",
  "tauri.js"
);
const generatedConfigPath = path.join(repoRoot, "tmp", "tauri.conf.generated.json");
const generatedBundlePath = path.join(tauriDir, "resources", "EmbeddedBundle");
const generatedBrandingPath = path.join(tauriDir, "resources", "branding.json");
const tauriReleaseBundlePath = path.join(tauriDir, "target", "release", "bundle");
const NO_SUFFIX_SENTINEL = "none";
const distributionSuffixes = [
  "Linux AppImage WebUI",
  "macOS WebUI",
  "Windows WebUI",
  "Ubuntu WebUI",
  "Fedora WebUI",
  "Linux WebUI",
  "Arch WebUI",
  "macOS",
  "WebUI",
];
const devConfig = await loadDevConfig();

if (isMainModule()) {
  await main();
}

export async function main(argv = process.argv.slice(2), platform = os.platform()) {
  if (argv.length === 0) {
    throw new Error("Usage: node scripts/run-tauri.mjs <tauri args...>");
  }

  await prepareBranding();
  try {
    await run(process.execPath, [tauriScript, ...argv, "-c", generatedConfigPath], {
      cwd: tauriDir,
      env: tauriChildEnv(process.env, platform),
    });
  } finally {
    await cleanupGeneratedFiles();
  }
}

async function prepareBranding() {
  const baseConfigPath = path.join(tauriDir, "tauri.conf.json");
  const baseConfig = JSON.parse(await readFile(baseConfigPath, "utf8"));
  const bundlePath = resolveEmbeddedBundlePath();
  const bundleMetadata = await loadBundleMetadata(bundlePath);
  const appName = tauriProductName(
    resolveAppName(bundlePath, baseConfig.productName),
    process.env.TAURI_PRODUCT_SUFFIX,
  );
  const appVersion = resolveAppVersion(bundleMetadata, baseConfig.version);
  const appIdentifier = resolveAppIdentifier(bundleMetadata, baseConfig.identifier);

  await mkdir(path.dirname(generatedConfigPath), { recursive: true });
  await rm(generatedBundlePath, { recursive: true, force: true });
  await rm(generatedBrandingPath, { force: true });
  if (process.env.TAURI_CLEAN_RELEASE_BUNDLE !== "0") {
    await rm(tauriReleaseBundlePath, { recursive: true, force: true });
  }

  const copied = await copyGitFiltered(bundlePath, generatedBundlePath, repoRoot);
  if (!copied) {
    await cp(bundlePath, generatedBundlePath, { recursive: true });
  }
  await writeFile(
    generatedBrandingPath,
    `${JSON.stringify(
      {
        appName,
        appVersion,
        appIdentifier,
        embeddedBundlePath: path.relative(repoRoot, bundlePath),
        embeddedBundleResourcePath: "examples/EmbeddedBundle",
      },
      null,
      2
    )}\n`,
    "utf8"
  );

  const generatedConfig = {
    ...baseConfig,
    productName: appName,
    version: appVersion,
    identifier: appIdentifier,
  };
  configureUpdater(generatedConfig);
  configureMacOSSigning(generatedConfig);
  await writeFile(generatedConfigPath, `${JSON.stringify(generatedConfig, null, 2)}\n`, "utf8");
  return { appName, appVersion, bundlePath };
}

function configureUpdater(config) {
  const updater = tauriUpdaterPluginConfig(process.env, devConfig);
  config.plugins ??= {};
  config.plugins.updater = updater;

  if (updater.pubkey && (process.env.TAURI_SIGNING_PRIVATE_KEY || parseBoolean(process.env.TAURI_CREATE_UPDATER_ARTIFACTS))) {
    config.bundle ??= {};
    config.bundle.createUpdaterArtifacts = true;
  }
}

export function tauriUpdaterPluginConfig(env = process.env, devConfigOverride = {}) {
  const pubkey = env.TAURI_UPDATER_PUBKEY
    || devConfigOverride.tauri?.updater?.pubkey
    || "";
  if (!pubkey) {
    return { pubkey: "", endpoints: [] };
  }

  return {
    pubkey,
    endpoints: updaterEndpoints(env, devConfigOverride),
    windows: {
      installMode: env.TAURI_UPDATER_WINDOWS_INSTALL_MODE
        || devConfigOverride.tauri?.updater?.windows_install_mode
        || "passive",
    },
  };
}

function configureMacOSSigning(config) {
  if (process.platform !== "darwin") {
    return;
  }

  const signingIdentity = effectiveMacOSSigningIdentity(process.env);
  if (!signingIdentity) {
    return;
  }

  config.bundle ??= {};
  config.bundle.macOS ??= {};
  config.bundle.macOS.signingIdentity = signingIdentity;
}

export function effectiveMacOSSigningIdentity(env = process.env, devConfigOverride = devConfig) {
  return env.TAURI_MACOS_SIGNING_IDENTITY
    || env.APPLE_SIGNING_IDENTITY
    || devConfigOverride.apple?.signing?.signing_identity
    || "";
}

export function tauriChildEnv(env = process.env, platform = process.platform, devConfigOverride = devConfig) {
  const childEnv = { ...env };
  if (platform === "darwin") {
    const signingIdentity = effectiveMacOSSigningIdentity(env, devConfigOverride);
    if (signingIdentity) {
      childEnv.APPLE_SIGNING_IDENTITY = signingIdentity;
      childEnv.TAURI_MACOS_SIGNING_IDENTITY = signingIdentity;
    }
  }
  return childEnv;
}

function updaterEndpoints(env = process.env, devConfigOverride = {}) {
  const configured = env.TAURI_UPDATER_ENDPOINTS
    || env.TAURI_UPDATER_ENDPOINT
    || devConfigOverride.tauri?.updater?.endpoints
    || devConfigOverride.tauri?.updater?.endpoint
    || "https://github.com/WGSExtract/wgsextract-cli/releases/latest/download/latest.json";
  return configured
    .split(/[\n,]/)
    .map((endpoint) => endpoint.trim())
    .filter(Boolean);
}

function parseBoolean(value) {
  return ["1", "true", "yes", "on"].includes(String(value || "").trim().toLowerCase());
}

// distributionSuffix: "none" intentionally disables suffix output; the sentinel is case-insensitive.
export function tauriProductName(appName, distributionSuffix) {
  const sanitizedSuffix = String(distributionSuffix ?? "").trim();
  const baseName = appNameWithoutDistributionSuffix(appName);
  if (sanitizedSuffix.toLowerCase() === NO_SUFFIX_SENTINEL) {
    return appNameWithDistributionSuffix(baseName, "");
  }
  return appNameWithDistributionSuffix(baseName, sanitizedSuffix);
}

function appNameWithoutDistributionSuffix(appName) {
  if (appName == null) {
    return null;
  }
  const strippedName = appName.trim();
  if (!strippedName) {
    return null;
  }
  const normalizedName = strippedName.toLowerCase();
  for (const suffix of [...distributionSuffixes].sort((left, right) => right.length - left.length)) {
    const normalizedSuffix = suffix.toLowerCase();
    if (normalizedName.endsWith(` ${normalizedSuffix}`)) {
      return strippedName.slice(0, -(suffix.length + 1)).trim() || strippedName;
    }
  }
  return strippedName;
}

function appNameWithDistributionSuffix(appName, suffix) {
  if (appName == null) {
    return null;
  }
  const strippedName = appName.trim();
  if (!strippedName) {
    return null;
  }
  const strippedSuffix = String(suffix ?? "").trim();
  if (!strippedSuffix) {
    return strippedName;
  }
  const normalizedName = strippedName.toLowerCase();
  const normalizedSuffix = strippedSuffix.toLowerCase();
  if (normalizedName.endsWith(` ${normalizedSuffix}`)) {
    return strippedName;
  }
  return `${strippedName} ${strippedSuffix}`;
}

function resolveEmbeddedBundlePath() {
  const configured = process.env.EMBEDDED_BUNDLE_PATH
    || process.env.PACKAGE_BUNDLE_PATH
    || devConfig.packaging?.embedded_bundle_path
    || "examples/WGSExtract";
  return resolveRepoPath(configured);
}

function resolveRepoPath(configuredPath) {
  const resolved = path.resolve(repoRoot, configuredPath);
  const relative = path.relative(repoRoot, resolved);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`Embedded bundle path must stay inside the repository: ${configuredPath}`);
  }
  return resolved;
}

function resolveAppName(bundlePath, defaultName) {
  const explicitAppName = process.env.PACKAGE_APP_NAME
    || process.env.EMBEDDED_APP_NAME
    || devConfig.packaging?.app_name;
  if (explicitAppName) {
    return explicitAppName;
  }
  return path.basename(bundlePath) || defaultName;
}

function resolveAppVersion(bundleMetadata, defaultVersion) {
  return process.env.PACKAGE_APP_VERSION
    || process.env.EMBEDDED_APP_VERSION
    || devConfig.packaging?.app_version
    || bundleMetadata.version
    || defaultVersion;
}

function resolveAppIdentifier(bundleMetadata, defaultIdentifier) {
  const explicitIdentifier = process.env.PACKAGE_APP_IDENTIFIER
    || process.env.EMBEDDED_APP_IDENTIFIER
    || devConfig.packaging?.app_identifier;
  if (explicitIdentifier) {
    return explicitIdentifier;
  }
  const id = bundleMetadata.id || "wgsextract";
  const normalizedId = id.replace(/[^a-zA-Z0-9]/g, "").toLowerCase() || "wgsextract";
  return `dev.guiforcli.web.embed.${normalizedId}`;
}

async function cleanupGeneratedFiles() {
  await rm(generatedConfigPath, { force: true });
  await rm(generatedBrandingPath, { force: true });
  await rm(generatedBundlePath, { recursive: true, force: true });
}

async function loadDevConfig() {
  try {
    const text = await readFile(devConfigPath, "utf8");
    return parseSimpleToml(text);
  } catch {
    return {};
  }
}

function parseSimpleToml(text) {
  const root = {};
  let section = root;
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) {
      continue;
    }
    if (line.startsWith("[") && line.endsWith("]")) {
      const parts = line.slice(1, -1).split(".").map((value) => value.trim()).filter(Boolean);
      section = root;
      for (const part of parts) {
        section[part] ??= {};
        section = section[part];
      }
      continue;
    }
    const separator = line.indexOf("=");
    if (separator < 0) {
      continue;
    }
    const key = line.slice(0, separator).trim();
    let value = line.slice(separator + 1).trim();
    if (value.startsWith('"')) {
      section[key] = parseQuotedString(value);
    } else {
      const commentIndex = value.indexOf(" #");
      if (commentIndex >= 0) {
        value = value.slice(0, commentIndex).trim();
      }
      section[key] = value;
    }
  }
  return root;
}

function parseQuotedString(value) {
  let escaped = false;
  for (let index = 1; index < value.length; index += 1) {
    const char = value[index];
    if (escaped) {
      escaped = false;
    } else if (char === "\\") {
      escaped = true;
    } else if (char === '"') {
      return value.slice(1, index).replace(/\\"/g, '"').replace(/\\\\/g, '\\');
    }
  }
  return value;
}

function run(command, commandArgs, options) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, commandArgs, { stdio: "inherit", ...options });
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

function isMainModule() {
  return Boolean(process.argv[1]) && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
}


async function copyGitFiltered(src, dest, repoRoot) {
  const relSrc = path.relative(repoRoot, src);
  if (relSrc.startsWith("..") || path.isAbsolute(relSrc)) {
    return false;
  }
  let stdout;
  try {
    ({ stdout } = await execFileAsync(
      "git",
      ["ls-files", "--cached", "--others", "--exclude-standard", "--", relSrc],
      { cwd: repoRoot }
    ));
  } catch (err) {
    console.warn(`Git-filtered copy failed for ${src}: ${err}`);
    return false;
  }
  const files = stdout.split(/\r?\n/).map((f) => f.trim()).filter(Boolean);
  if (files.length === 0) {
    return false;
  }
  await rm(dest, { recursive: true, force: true });
  for (const f of files) {
    const fileSrc = path.join(repoRoot, f);
    const relToSrc = path.relative(src, fileSrc);
    if (relToSrc.startsWith("..") || path.isAbsolute(relToSrc)) {
      continue;
    }
    if (!existsSync(fileSrc)) {
      // Tracked files deleted in the working tree are still listed by `git ls-files --cached`.
      continue;
    }
    const fileDest = path.join(dest, relToSrc);
    await mkdir(path.dirname(fileDest), { recursive: true });
    await cp(fileSrc, fileDest);
  }
  return true;
}
