import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import type { WorkspaceLocationInfo } from "../../../shared/types.js";
import { errnoCode, errorMessage, HttpError } from "./errors.js";
import { appSupportDirectory, safePathComponent } from "./paths.js";

export const workspacesDirectoryEnvName = "GUI_FOR_CLI_BUNDLE_WORKSPACES_DIR";
export const workspaceSentinelFileName = ".bundle-workspace";
export const workspaceSentinelContents = "GUI for CLI bundle workspace\n";

type WorkspacesDirectorySource = "environment" | "settings" | "default";

/** App-wide settings. They live beside the workspaces, not in them, since they say where the workspaces are. */
export function appSettingsPath() {
    return path.join(appSupportDirectory(), "settings.json");
}

export function defaultWorkspacesDirectory() {
    return path.join(appSupportDirectory(), "BundleWorkspaces");
}

async function readAppSettings(): Promise<Record<string, unknown>> {
    try {
        const parsed: unknown = JSON.parse(await readFile(appSettingsPath(), "utf8"));
        return parsed != null && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {};
    }
    catch (error) {
        if (errnoCode(error) !== "ENOENT") {
            console.error(`Ignoring unreadable ${appSettingsPath()}: ${errorMessage(error)}`);
        }
        return {};
    }
}

async function configuredWorkspacesDirectory() {
    const value = (await readAppSettings()).bundleWorkspacesDirectory;
    return typeof value === "string" && path.isAbsolute(value) ? value : null;
}

/** The folder holding one workspace per bundle: the environment override, then the saved setting, then the default. */
export async function resolveWorkspacesDirectory(): Promise<{ directory: string; source: WorkspacesDirectorySource }> {
    const fromEnvironment = process.env[workspacesDirectoryEnvName]?.trim();
    if (fromEnvironment) {
        return { directory: path.resolve(fromEnvironment), source: "environment" };
    }
    const configured = await configuredWorkspacesDirectory();
    return configured ? { directory: configured, source: "settings" } : { directory: defaultWorkspacesDirectory(), source: "default" };
}

export function workspaceRootIn(directory: string, bundleID: unknown) {
    return path.join(directory, safePathComponent(bundleID));
}

/**
 * Syncing replaces entries named like the bundle's, so a chosen folder may only hold this bundle's
 * workspace if it is empty or already a workspace; someone else's folder of that name is never adopted.
 */
export async function assertAdoptableWorkspaceRoot(workspaceRoot: string) {
    const entries = await readdir(workspaceRoot).catch((error: unknown) => {
        if (errnoCode(error) === "ENOENT") {
            return [];
        }
        throw error;
    });
    if (entries.length > 0 && !(await isManagedWorkspace(workspaceRoot))) {
        throw new Error(`Refusing to use non-empty folder that is not a bundle workspace: ${workspaceRoot}`);
    }
}

export async function isManagedWorkspace(workspaceRoot: string) {
    const sentinel = await readFile(path.join(workspaceRoot, workspaceSentinelFileName), "utf8").catch(() => null);
    return sentinel === workspaceSentinelContents;
}

export async function workspaceLocationInfo(bundleID: unknown, workspaceRoot: string): Promise<WorkspaceLocationInfo> {
    const { directory, source } = await resolveWorkspacesDirectory();
    return {
        workspaceRoot,
        defaultDirectory: defaultWorkspacesDirectory(),
        configuredDirectory: await configuredWorkspacesDirectory(),
        environmentOverride: source === "environment",
        restartRequired: path.resolve(workspaceRootIn(directory, bundleID)) !== path.resolve(workspaceRoot),
    };
}

/** Saves the workspaces folder for the next launch, or clears it when null. Nothing is moved. */
export async function saveWorkspacesDirectory(requested: string | null, bundleID: unknown, sourceRoot: string, workspaceRoot: string) {
    const settings = await readAppSettings();
    if (requested == null) {
        delete settings.bundleWorkspacesDirectory;
    }
    else {
        await assertUsableWorkspacesDirectory(requested, bundleID, sourceRoot, workspaceRoot);
        settings.bundleWorkspacesDirectory = path.resolve(requested);
    }
    await mkdir(appSupportDirectory(), { recursive: true });
    await writeFile(appSettingsPath(), `${JSON.stringify(settings, null, 2)}\n`, "utf8");
}

async function assertUsableWorkspacesDirectory(directory: string, bundleID: unknown, sourceRoot: string, workspaceRoot: string) {
    if (!path.isAbsolute(directory)) {
        throw new HttpError(400, `The workspaces folder must be an absolute path: ${directory}`);
    }
    for (const root of [sourceRoot, workspaceRoot]) {
        if (isSameOrInside(root, directory)) {
            throw new HttpError(400, `The workspaces folder cannot be inside ${root}`);
        }
    }
    try {
        await mkdir(directory, { recursive: true });
        await assertAdoptableWorkspaceRoot(workspaceRootIn(directory, bundleID));
    }
    catch (error) {
        throw new HttpError(400, errorMessage(error));
    }
}

function isSameOrInside(parent: string, candidate: string) {
    const relative = path.relative(parent, candidate);
    return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}
