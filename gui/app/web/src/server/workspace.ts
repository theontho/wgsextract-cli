import type { Dirent } from "node:fs";
import { chmod, cp, mkdir, open, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { appSupportDirectory, relativeTopLevelName, resolveUserPath, safePathComponent } from "./paths.js";
import { configEditorControls } from "../../../shared/rendering.js";
import type { BundleManifest } from "../../../shared/types.js";
import { errnoCode } from "./errors.js";

interface EntryFingerprint {
    path: string;
    kind: string;
    size?: number;
    mode: number;
    mtimeMs: number;
}

interface WorkspaceFingerprint {
    version: number;
    manifestID: string | undefined;
    sourceRoot: string;
    preservedNames: string[];
    entries: EntryFingerprint[];
}

const syncMetadataFileName = ".workspace-sync.json";
const workspaceSentinelFileName = ".bundle-workspace";
const workspaceSentinelContents = "GUI for CLI bundle workspace\n";
const syncMetadataVersion = 1;

export async function prepareBundleWorkspace(manifest: BundleManifest, sourceRoot: string, explicitWorkspaceRoot: string | undefined = undefined) {
    const workspaceRoot = explicitWorkspaceRoot
        ? path.resolve(explicitWorkspaceRoot)
        : path.join(appSupportDirectory(), "BundleWorkspaces", safePathComponent(manifest.id));
    if (explicitWorkspaceRoot) {
        await assertSafeExplicitWorkspaceRoot(workspaceRoot, manifest, sourceRoot);
    }
    await mkdir(workspaceRoot, { recursive: true });
    await writeWorkspaceSentinel(workspaceRoot);
    const sourceEntries = (await readdir(sourceRoot, { withFileTypes: true })).filter((entry) => !entry.name.startsWith("."));
    const sourceNames = new Set(sourceEntries.map((entry) => entry.name));
    const preservedNames = preservedWorkspaceEntryNames(manifest, workspaceRoot);
    const fingerprint = await workspaceSyncFingerprint(manifest, sourceRoot, sourceEntries, preservedNames);
    await removeStaleWorkspaceEntries(workspaceRoot, sourceNames, preservedNames, await previouslySyncedNames(workspaceRoot));
    if (await isWorkspaceCurrent(workspaceRoot, fingerprint)) {
        await markDemoScriptsExecutable(workspaceRoot);
        return workspaceRoot;
    }
    await syncSourceEntries(sourceRoot, workspaceRoot, sourceEntries, preservedNames);
    await markDemoScriptsExecutable(workspaceRoot);
    await writeWorkspaceSyncMetadata(workspaceRoot, fingerprint);
    return workspaceRoot;
}

async function syncSourceEntries(sourceRoot: string, workspaceRoot: string, sourceEntries: Dirent[], preservedNames: Set<string>) {
    for (const entry of sourceEntries) {
        if (preservedNames.has(entry.name) && (await pathExists(path.join(workspaceRoot, entry.name)))) {
            continue;
        }
        const source = path.join(sourceRoot, entry.name);
        const destination = path.join(workspaceRoot, entry.name);
        await rm(destination, { recursive: true, force: true });
        await cp(source, destination, { recursive: true });
    }
}

async function pathExists(candidate: string) {
    try {
        await stat(candidate);
        return true;
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT") {
            return false;
        }
        throw error;
    }
}

/**
 * Removes what an earlier sync copied from the bundle and the bundle no longer has. Everything else in
 * the workspace is the user's or the bundle's own data (a reference library, genomes, outputs) and stays.
 */
async function removeStaleWorkspaceEntries(workspaceRoot: string, sourceNames: Set<string>, preservedNames: Set<string>, syncedNames: Set<string>) {
    for (const name of syncedNames) {
        if (!sourceNames.has(name) && !preservedNames.has(name)) {
            await rm(path.join(workspaceRoot, name), { recursive: true, force: true });
        }
    }
}

/** The top-level entries the last sync copied from the bundle. */
async function previouslySyncedNames(workspaceRoot: string): Promise<Set<string>> {
    const metadata = await readWorkspaceSyncMetadata(workspaceRoot).catch(() => null);
    const entries = Array.isArray(metadata?.entries) ? metadata.entries : [];
    const names = entries.map((entry) => String(entry.path ?? "").split(/[\\/]/)[0] ?? "");
    return new Set(names.filter((name) => name && name !== "." && name !== ".." &&
        name !== syncMetadataFileName && name !== workspaceSentinelFileName));
}

async function assertSafeExplicitWorkspaceRoot(workspaceRoot: string, manifest: BundleManifest, sourceRoot: string) {
    const entries = await readdir(workspaceRoot, { withFileTypes: true }).catch((error) => {
        if (errnoCode(error) === "ENOENT") {
            return [];
        }
        throw error;
    });
    if (entries.length === 0) {
        return;
    }
    const metadata = await readWorkspaceSyncMetadata(workspaceRoot).catch(() => null);
    const sentinel = await readFile(path.join(workspaceRoot, workspaceSentinelFileName), "utf8").catch(() => null);
    const managed = sentinel === workspaceSentinelContents &&
        metadata?.version === syncMetadataVersion &&
        metadata?.manifestID === manifest.id &&
        metadata?.sourceRoot === path.resolve(sourceRoot);
    if (!managed) {
        throw new Error(`Refusing to use non-empty unmanaged workspace root: ${workspaceRoot}`);
    }
}

async function writeWorkspaceSentinel(workspaceRoot: string) {
    await writeFile(path.join(workspaceRoot, workspaceSentinelFileName), workspaceSentinelContents, "utf8");
}

async function isWorkspaceCurrent(workspaceRoot: string, fingerprint: WorkspaceFingerprint) {
    try {
        const current = await readWorkspaceSyncMetadata(workspaceRoot);
        return JSON.stringify(current) === JSON.stringify(fingerprint) && (await copiedSourceEntriesExist(workspaceRoot, fingerprint.entries));
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT") {
            return false;
        }
        throw error;
    }
}

async function readWorkspaceSyncMetadata(workspaceRoot: string): Promise<Partial<WorkspaceFingerprint> | null> {
    return JSON.parse(await readFile(workspaceSyncMetadataPath(workspaceRoot), "utf8"));
}

async function copiedSourceEntriesExist(workspaceRoot: string, entries: EntryFingerprint[]) {
    for (const entry of entries) {
        if (entry.kind === "directory") {
            continue;
        }
        if (entry.path === "runtime" || entry.path.startsWith(`runtime${path.sep}`)) {
            continue;
        }
        try {
            const info = await stat(path.join(workspaceRoot, entry.path));
            if (!info.isFile() || info.size !== entry.size) {
                return false;
            }
        }
        catch (error) {
            if (errnoCode(error) === "ENOENT") {
                return false;
            }
            throw error;
        }
    }
    return true;
}

async function writeWorkspaceSyncMetadata(workspaceRoot: string, fingerprint: WorkspaceFingerprint) {
    await writeFile(workspaceSyncMetadataPath(workspaceRoot), `${JSON.stringify(fingerprint, null, 2)}\n`, "utf8");
}

function workspaceSyncMetadataPath(workspaceRoot: string) {
    return path.join(workspaceRoot, syncMetadataFileName);
}

async function workspaceSyncFingerprint(manifest: BundleManifest, sourceRoot: string, sourceEntries: Dirent[], preservedNames: Set<string>): Promise<WorkspaceFingerprint> {
    return {
        version: syncMetadataVersion,
        manifestID: manifest.id,
        sourceRoot: path.resolve(sourceRoot),
        preservedNames: [...preservedNames].sort(),
        entries: await sourceEntryFingerprints(sourceRoot, sourceEntries, "", preservedNames),
    };
}

async function sourceEntryFingerprints(root: string, entries: Dirent[], prefix = "", preservedNames = new Set<string>()): Promise<EntryFingerprint[]> {
    const fingerprints: EntryFingerprint[] = [];
    const visibleEntries = entries
        .filter((entry) => !entry.name.startsWith("."))
        .sort((first, second) => first.name.localeCompare(second.name));
    for (const entry of visibleEntries) {
        const relativePath = prefix ? path.join(prefix, entry.name) : entry.name;
        if (!prefix && preservedNames.has(entry.name)) {
            continue;
        }
        const filePath = path.join(root, relativePath);
        const info = await stat(filePath);
        if (entry.isDirectory()) {
            fingerprints.push({
                path: relativePath,
                kind: "directory",
                mode: info.mode,
                mtimeMs: Math.trunc(info.mtimeMs),
            });
            fingerprints.push(...(await sourceEntryFingerprints(root, await readdir(filePath, { withFileTypes: true }), relativePath, preservedNames)));
            continue;
        }
        if (entry.isFile()) {
            fingerprints.push({
                path: relativePath,
                kind: "file",
                size: info.size,
                mode: info.mode,
                mtimeMs: Math.trunc(info.mtimeMs),
            });
        }
    }
    return fingerprints;
}

export function preservedWorkspaceEntryNames(manifest: BundleManifest, workspaceRoot: string): Set<string> {
    const names = new Set(["runtime", "state.json"]);
    for (const control of configEditorControls(manifest)) {
        const rawPath = control.configFile?.path;
        if (!rawPath) {
            continue;
        }
        const topLevelName = relativeTopLevelName(workspaceRoot, resolveUserPath(rawPath, workspaceRoot));
        if (topLevelName) {
            names.add(topLevelName);
        }
    }
    return names;
}
async function markDemoScriptsExecutable(root: string) {
    const scriptsRoot = path.join(root, "scripts");
    for (const scriptName of await scriptFiles(scriptsRoot)) {
        try {
            await chmod(path.join(scriptsRoot, scriptName), 0o755);
        }
        catch (error) {
            if (errnoCode(error) !== "ENOENT")
                throw error;
        }
    }
}
async function scriptFiles(root: string, prefix = ""): Promise<string[]> {
    let entries: Dirent[];
    try {
        entries = await readdir(path.join(root, prefix), { withFileTypes: true });
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT" || errnoCode(error) === "ENOTDIR") {
            return [];
        }
        throw error;
    }
    const files: string[] = [];
    for (const entry of entries) {
        const relative = path.join(prefix, entry.name);
        if (entry.isDirectory()) {
            files.push(...await scriptFiles(root, relative));
        }
        else if (entry.isFile() && (await isExecutableScriptFile(root, relative))) {
            files.push(relative);
        }
    }
    return files;
}
async function isExecutableScriptFile(root: string, scriptPath: string) {
    const extension = path.extname(scriptPath).toLowerCase();
    if (extension === ".sh" || extension === ".py" || extension === ".ps1" || extension === ".cmd" || extension === ".bat") {
        return true;
    }
    return extension === "" && await hasShebang(path.join(root, scriptPath));
}
async function hasShebang(filePath: string) {
    const handle = await open(filePath, "r");
    try {
        const buffer = Buffer.alloc(2);
        const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
        return bytesRead === 2 && buffer[0] === 0x23 && buffer[1] === 0x21;
    }
    finally {
        await handle.close();
    }
}
