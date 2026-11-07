import { access, readdir, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import type { BundleManifest, LooseRecord, SetupStep } from "../../../shared/types.js";
import { setupStepAppliesToPlatform, type SetupPlatform } from "../../../shared/setup-platforms.js";
import { resolveBundlePath } from "./paths.js";

const scriptRootName = "scripts";
const platformFolderNames = new Set(["windows", "posix", "macos", "linux"]);
const windowsExtensions = [".ps1", ".cmd", ".bat", ".py"];
const posixExtensions = [".sh", ".py"];

export async function resolvePlatformScriptPath(scriptPath: string, bundleRoot: string): Promise<string> {
  const normalized = normalizeScriptPath(relativeToBundleRoot(scriptPath, bundleRoot));
  const fallback = resolveBundlePath(normalized, bundleRoot);
  if (!isBundleScriptPath(normalized)) {
    return fallback;
  }

  for (const candidate of await platformScriptCandidates(normalized)) {
    const safeCandidate = resolveBundlePath(candidate, bundleRoot);
    if (await exists(safeCandidate)) {
      return safeCandidate;
    }
  }

  return fallback;
}

export function isPlatformScriptReference(scriptPath: string, bundleRoot: string): boolean {
  return isBundleScriptPath(normalizeScriptPath(relativeToBundleRoot(scriptPath, bundleRoot)));
}

export async function validatePlatformScriptSets(bundleRoot: string, manifest: BundleManifest): Promise<void> {
  if (referencedScriptStems(manifest).size === 0) {
    return;
  }

  const scriptsRoot = path.join(bundleRoot, scriptRootName);
  const folders = await platformScriptSetFolders(scriptsRoot);
  if (folders.length === 0) {
    return;
  }
  const shared = await scriptStemsInDirectory(scriptsRoot);
  for (const folder of folders) {
    const required = referencedScriptStems(manifest, platformsForScriptFolder(folder, scriptsRoot));
    if (required.size === 0) {
      continue;
    }
    const present = new Set([...shared, ...await scriptStemsInDirectory(folder)]);
    const missing = [...required].filter((stem) => !present.has(stem));
    if (missing.length > 0) {
      throw new Error(
        `Platform script folder ${path.relative(bundleRoot, folder)} is missing required scripts: ${missing.join(", ")}`
      );
    }
  }
}

async function scriptStemsInDirectory(directory: string): Promise<string[]> {
  const entries = await readdir(directory, { withFileTypes: true });
  return entries.filter((entry) => entry.isFile()).map((entry) => scriptStem(entry.name)).filter(Boolean);
}

export function referencedScriptStems(manifest: BundleManifest, platforms?: SetupPlatform | SetupPlatform[]): Set<string> {
  const stems = new Set<string>();
  for (const value of referencedScriptPaths(manifest, platforms)) {
    const stem = scriptStem(path.basename(normalizeScriptPath(value)));
    if (stem) {
      stems.add(stem);
    }
  }
  return stems;
}

async function platformScriptCandidates(scriptPath: string): Promise<string[]> {
  const { logicalDirectory, fileName } = scriptParts(scriptPath);
  const platformDirectories = await platformScriptDirectories(logicalDirectory);
  const extensions = platformExtensions(fileName);
  const stem = scriptStem(fileName);
  return [
    ...platformDirectories.flatMap((directory) => extensions.map((extension) => path.join(directory, `${stem}${extension}`))),
    scriptPath,
  ];
}

async function platformScriptDirectories(logicalDirectory: string): Promise<string[]> {
  const platform = os.platform();
  if (platform === "win32") {
    return [path.join(logicalDirectory, "windows"), path.join(logicalDirectory, "posix")];
  }
  if (platform === "darwin") {
    return [path.join(logicalDirectory, "macos"), path.join(logicalDirectory, "posix")];
  }
  if (platform === "linux") {
    const distro = await linuxDistroID();
    return [
      ...(distro ? [path.join(logicalDirectory, "linux", distro)] : []),
      path.join(logicalDirectory, "linux"),
      path.join(logicalDirectory, "posix"),
    ];
  }
  return [path.join(logicalDirectory, "posix")];
}

function platformExtensions(fileName: string): string[] {
  const extension = path.extname(fileName).toLowerCase();
  if (os.platform() === "win32") {
    return extension === ".py" ? [".py", ...windowsExtensions.filter((item) => item !== ".py")] : windowsExtensions;
  }
  return extension === ".py" ? [".py", ...posixExtensions.filter((item) => item !== ".py")] : posixExtensions;
}

async function platformScriptSetFolders(scriptsRoot: string): Promise<string[]> {
  if (!(await exists(scriptsRoot))) {
    return [];
  }
  const folders: string[] = [];
  for (const name of platformFolderNames) {
    const folder = path.join(scriptsRoot, name);
    if (!(await isDirectory(folder))) {
      continue;
    }
    const entries = await readdir(folder, { withFileTypes: true });
    const childFolders = entries.filter((entry) => entry.isDirectory()).map((entry) => path.join(folder, entry.name));
    if (entries.some((entry) => entry.isFile()) || childFolders.length === 0) {
      folders.push(folder);
    }
    folders.push(...childFolders);
  }
  return folders;
}

function* referencedScriptPaths(manifest: BundleManifest, platforms?: SetupPlatform | SetupPlatform[]): Iterable<string> {
  for (const steps of [manifest.setup?.steps ?? [], manifest.uninstall?.steps ?? []]) {
    for (const step of steps) {
      if (
        (step.kind === "setupScript" || step.kind === "bundledScript") &&
        isBundleScriptPath(step.value) &&
        (platforms == null || setupStepAppliesToAnyPlatform(step, platforms))
      ) {
        yield step.value;
      }
    }
  }
  for (const page of manifest.pages ?? []) {
    yield* pageScriptPaths(page);
  }
}

function* pageScriptPaths(value: unknown): Iterable<string> {
  if (!value || typeof value !== "object") {
    return;
  }
  const record = value as LooseRecord & {
    dataSource?: { path?: unknown };
    command?: { executable?: unknown };
  };
  if (isBundleScriptPath(record.dataSource?.path)) {
    yield record.dataSource.path;
  }
  if (isBundleScriptPath(record.command?.executable)) {
    yield record.command.executable;
  }
  for (const child of Object.values(record)) {
    if (Array.isArray(child)) {
      for (const item of child) {
        yield* pageScriptPaths(item);
      }
    } else if (child && typeof child === "object") {
      yield* pageScriptPaths(child);
    }
  }
}

function scriptParts(scriptPath: string): { logicalDirectory: string; fileName: string } {
  const parts = normalizeScriptPath(scriptPath).split("/");
  const scriptsIndex = parts.indexOf(scriptRootName);
  const next = parts[scriptsIndex + 1];
  const hasPlatformDirectory = next === "linux"
    ? parts.length > scriptsIndex + 3
    : next != null && platformFolderNames.has(next);
  const fileName = parts.at(-1) ?? "";
  const logicalParts = hasPlatformDirectory
    ? parts.slice(0, scriptsIndex + 1)
    : parts.slice(0, -1);
  return { logicalDirectory: logicalParts.join("/"), fileName };
}

function isBundleScriptPath(value: unknown): value is string {
  if (typeof value !== "string") {
    return false;
  }
  const normalized = normalizeScriptPath(value);
  return normalized.startsWith(`${scriptRootName}/`) && !hasParentSegment(normalized) && !path.isAbsolute(normalized);
}

function normalizeScriptPath(value: string): string {
  return value
    .replaceAll("\\", "/")
    .replace(/^\{\{bundleRoot\}\}\//, "")
    .replace(/^\.?\//, "");
}

function hasParentSegment(value: string): boolean {
  return normalizeScriptPath(value).split("/").includes("..");
}

function relativeToBundleRoot(value: string, bundleRoot: string): string {
  if (!path.isAbsolute(value)) {
    return value;
  }
  const relative = path.relative(bundleRoot, value);
  return relative && !relative.startsWith("..") && !path.isAbsolute(relative) ? relative : value;
}

function scriptStem(fileName: string): string {
  return path.basename(fileName, path.extname(fileName));
}

function platformsForScriptFolder(folder: string, scriptsRoot: string): SetupPlatform[] {
  const relative = path.relative(scriptsRoot, folder).replaceAll(path.sep, "/");
  if (relative === "windows" || relative.startsWith("windows/")) {
    return ["windows"];
  }
  if (relative === "macos" || relative.startsWith("macos/")) {
    return ["macos"];
  }
  if (relative === "posix" || relative.startsWith("posix/")) {
    return ["macos", "linux", "posix"];
  }
  return relative === "linux" || relative.startsWith("linux/") ? ["linux"] : ["posix"];
}

function setupStepAppliesToAnyPlatform(step: SetupStep, platforms: SetupPlatform | SetupPlatform[]): boolean {
  return (Array.isArray(platforms) ? platforms : [platforms]).some((platform) => setupStepAppliesToPlatform(step, platform));
}

async function linuxDistroID(): Promise<string | undefined> {
  const override = process.env.GUI_FOR_CLI_LINUX_DISTRO || process.env.ID;
  if (override?.trim()) {
    return sanitizeDistroID(override);
  }
  try {
    const osRelease = await import("node:fs/promises").then(({ readFile }) => readFile("/etc/os-release", "utf8"));
    const id = /^ID=(.*)$/m.exec(osRelease)?.[1]?.replace(/^"|"$/g, "");
    return id ? sanitizeDistroID(id) : undefined;
  } catch (error) {
    if (isNodeErrorWithCode(error, ["ENOENT", "ENOTDIR", "EACCES"])) {
      return undefined;
    }
    throw error;
  }
}

function sanitizeDistroID(value: string): string | undefined {
  const sanitized = value.trim().toLowerCase().replace(/[^a-z0-9._-]+/g, "-");
  return sanitized && sanitized !== "." && sanitized !== ".." ? sanitized : undefined;
}

async function exists(filePath: string): Promise<boolean> {
  try {
    await access(filePath);
    return true;
  } catch (error) {
    if (isNodeErrorWithCode(error, ["ENOENT", "ENOTDIR"])) {
      return false;
    }
    throw error;
  }
}

async function isDirectory(filePath: string): Promise<boolean> {
  try {
    return (await stat(filePath)).isDirectory();
  } catch (error) {
    if (isNodeErrorWithCode(error, ["ENOENT", "ENOTDIR"])) {
      return false;
    }
    throw error;
  }
}

function isNodeErrorWithCode(error: unknown, codes: string[]): error is NodeJS.ErrnoException {
  return error instanceof Error && "code" in error && codes.includes(String(error.code));
}
