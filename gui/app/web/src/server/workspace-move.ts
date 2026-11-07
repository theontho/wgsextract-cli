import { copyFile, lstat, mkdir, readFile, readdir, readlink, rename, rm, rmdir, statfs, symlink, utimes } from "node:fs/promises";
import path from "node:path";
import { configEditorControls, parseFlatToml, serializeFlatToml } from "../../../shared/rendering.js";
import type { BundleManifest, ValueMap } from "../../../shared/types.js";
import { relocatePath } from "../../../shared/workspace-paths.js";
import { bundleStatePath, writeFileAtomically } from "./config-store.js";
import { errnoCode, HttpError } from "./errors.js";
import { configPath } from "./paths.js";
import { defaultWorkspaceRoot, resolveWorkspaceRoot, saveWorkspacesDirectory, workspaceRootIn } from "./workspace-location.js";

export interface WorkspaceMoveRequest {
    manifest: BundleManifest;
    sourceRoot: string;
    workspaceRoot: string;
    /** The folder to hold the workspace, or null for the default location. */
    directory: string | null;
    onProgress: (percent: number) => void | Promise<void>;
    signal: AbortSignal;
}

const runtimeFolderName = "runtime";
// What setup installed outside the runtime (Pixi, MSYS2), which its next run and uninstall still need to know about.
const keptRuntimeFileName = "install-manifest.json";

/**
 * Moves the workspace and everything in it, then records the new location. The original stays whole until
 * the new one is complete, so a failure or cancelled copy loses nothing. The CLI runtime isn't carried over:
 * its environments and launchers hold absolute paths, so setup installs it again in the new location.
 */
export async function moveWorkspace({ manifest, sourceRoot, workspaceRoot, directory, onProgress, signal }: WorkspaceMoveRequest): Promise<void> {
    if ((await resolveWorkspaceRoot(manifest)).source === "environment") {
        throw new HttpError(400, "The workspace folder is set by an environment variable.");
    }
    if (directory != null && !path.isAbsolute(directory)) {
        throw new HttpError(400, `The workspaces folder must be an absolute path: ${directory}`);
    }
    const target = directory == null ? defaultWorkspaceRoot(manifest) : workspaceRootIn(path.resolve(directory), manifest.id);
    if (path.relative(workspaceRoot, target) === "") {
        await saveWorkspacesDirectory(directory == null ? null : path.resolve(directory));
        return;
    }
    await assertUsableTarget(target, sourceRoot, workspaceRoot);
    await mkdir(path.dirname(target), { recursive: true });
    await rmdir(target).catch(ignoreMissing);

    const renamed = await renameIfSameVolume(workspaceRoot, target);
    if (!renamed) {
        await copyWorkspace(workspaceRoot, target, onProgress, signal);
    }
    try {
        await relocateWorkspacePaths(manifest, target, workspaceRoot, target);
        await saveWorkspacesDirectory(directory == null ? null : path.resolve(directory));
    }
    catch (error) {
        if (renamed) {
            await rename(target, workspaceRoot);
            await relocateWorkspacePaths(manifest, workspaceRoot, target, workspaceRoot);
        }
        else {
            await rm(target, { recursive: true, force: true });
        }
        throw error;
    }
    if (renamed) {
        await discardRuntime(target);
    }
    else {
        await rm(workspaceRoot, { recursive: true, force: true, maxRetries: 3 }).catch((error: unknown) => {
            console.warn(`Could not remove the old workspace at ${workspaceRoot}: ${String(error)}`);
        });
    }
    await onProgress(100);
}

async function assertUsableTarget(target: string, sourceRoot: string, workspaceRoot: string) {
    for (const root of [sourceRoot, workspaceRoot]) {
        if (isSameOrInside(root, target)) {
            throw new HttpError(400, `The workspaces folder cannot be inside ${root}`);
        }
    }
    const entries = await readdir(target).catch((error: unknown) => {
        if (errnoCode(error) === "ENOENT") {
            return [];
        }
        throw new HttpError(400, `Cannot use ${target}: ${String(error)}`);
    });
    if (entries.length > 0) {
        throw new HttpError(400, `${target} already has files in it. Choose another folder, or move or delete them first.`);
    }
}

function isSameOrInside(parent: string, candidate: string) {
    const relative = path.relative(parent, candidate);
    return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function ignoreMissing(error: unknown) {
    if (errnoCode(error) !== "ENOENT") {
        throw error;
    }
}

async function renameIfSameVolume(from: string, to: string) {
    try {
        await rename(from, to);
        return true;
    }
    catch (error) {
        if (errnoCode(error) === "EXDEV") {
            return false;
        }
        throw new HttpError(409, `Could not move ${from}. Close anything using files in it and try again: ${String(error)}`);
    }
}

/** Copies through a staging folder renamed into place at the end, so the target is never partly there. */
async function copyWorkspace(from: string, to: string, onProgress: WorkspaceMoveRequest["onProgress"], signal: AbortSignal) {
    const staging = `${to}.moving`;
    await rm(staging, { recursive: true, force: true });
    await mkdir(staging, { recursive: true });
    try {
        const files = await listCarriedFiles(from);
        const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
        const volume = await statfs(staging);
        const availableBytes = Number(volume.bavail) * Number(volume.bsize);
        if (totalBytes > availableBytes) {
            throw new HttpError(409, `Moving needs ${formatGB(totalBytes)} GB, but ${path.dirname(to)} has ${formatGB(availableBytes)} GB free.`);
        }
        let copiedBytes = 0;
        let reportedPercent = 0;
        for (const file of files) {
            signal.throwIfAborted();
            await copyEntry(from, staging, file.relative);
            copiedBytes += file.size;
            const percent = totalBytes ? Math.floor(copiedBytes / totalBytes * 99) : 99;
            if (percent > reportedPercent) {
                reportedPercent = percent;
                await onProgress(percent);
            }
        }
        await rename(staging, to);
    }
    catch (error) {
        await rm(staging, { recursive: true, force: true });
        throw error;
    }
}

function formatGB(bytes: number) {
    return (bytes / 1024 ** 3).toFixed(1);
}

async function listCarriedFiles(root: string, relative = ""): Promise<Array<{ relative: string; size: number }>> {
    const files: Array<{ relative: string; size: number }> = [];
    for (const entry of await readdir(path.join(root, relative), { withFileTypes: true })) {
        const entryPath = path.join(relative, entry.name);
        if (isDiscardedRuntimePath(entryPath)) {
            continue;
        }
        if (entry.isDirectory()) {
            files.push(...await listCarriedFiles(root, entryPath));
        }
        else {
            files.push({ relative: entryPath, size: (await lstat(path.join(root, entryPath))).size });
        }
    }
    return files;
}

function isDiscardedRuntimePath(relative: string) {
    const parts = relative.split(path.sep);
    // runtime/<tool> itself is walked; only its kept file comes along.
    return parts[0] === runtimeFolderName && parts.length >= 3 && !(parts.length === 3 && parts[2] === keptRuntimeFileName);
}

async function copyEntry(fromRoot: string, toRoot: string, relative: string) {
    const from = path.join(fromRoot, relative);
    const to = path.join(toRoot, relative);
    await mkdir(path.dirname(to), { recursive: true });
    const info = await lstat(from);
    if (info.isSymbolicLink()) {
        await symlink(await readlink(from), to);
        return;
    }
    await copyFile(from, to);
    await utimes(to, info.atime, info.mtime);
}

/** Removes what a renamed workspace's runtime can't use in its new location. */
async function discardRuntime(root: string) {
    const runtime = path.join(root, runtimeFolderName);
    for (const tool of await readdir(runtime).catch(() => [])) {
        for (const entry of await readdir(path.join(runtime, tool)).catch(() => [])) {
            if (entry !== keptRuntimeFileName) {
                await rm(path.join(runtime, tool, entry), { recursive: true, force: true, maxRetries: 3 });
            }
        }
    }
}

/** Saved absolute paths into the workspace follow it. Setup's saved run is cleared, since setup must run again. */
async function relocateWorkspacePaths(manifest: BundleManifest, root: string, from: string, to: string) {
    const statePath = bundleStatePath(root);
    const savedState = await readFile(statePath, "utf8").then((text) => JSON.parse(text) as Record<string, unknown>).catch((error: unknown) => {
        if (errnoCode(error) === "ENOENT") {
            return null;
        }
        throw error;
    });
    const configFilePaths = (savedState?.configFilePaths ?? {}) as Record<string, string | undefined>;
    for (const control of configEditorControls(manifest)) {
        if (control.configFile) {
            const requested = configFilePaths[control.id];
            await relocateConfigFile(configPath(control, requested ? relocatePath(requested, from, to) : undefined, root), from, to);
        }
    }
    if (savedState) {
        const relocated = relocateStrings(savedState, from, to) as Record<string, unknown>;
        await writeFileAtomically(statePath, `${JSON.stringify({ ...relocated, setupRun: null }, null, 2)}\n`);
    }
}

async function relocateConfigFile(filePath: string, from: string, to: string) {
    const text = await readFile(filePath, "utf8").catch((error: unknown) => {
        ignoreMissing(error);
        return null;
    });
    if (text != null) {
        await writeFileAtomically(filePath, serializeFlatToml(relocateStrings(parseFlatToml(text), from, to) as ValueMap));
    }
}

function relocateStrings(value: unknown, from: string, to: string): unknown {
    if (typeof value === "string") {
        return relocatePath(value, from, to);
    }
    if (Array.isArray(value)) {
        return value.map((item) => relocateStrings(item, from, to));
    }
    if (value != null && typeof value === "object") {
        return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, relocateStrings(item, from, to)]));
    }
    return value;
}
