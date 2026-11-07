import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";
import type { BundleManifest, WorkspaceLocationInfo } from "../../../shared/types.js";
import { errnoCode, errorMessage } from "./errors.js";
import { appSupportDirectory, safePathComponent } from "./paths.js";

export const workspacesDirectoryEnvName = "GUI_FOR_CLI_BUNDLE_WORKSPACES_DIR";
export const workspaceSentinelFileName = ".bundle-workspace";
export const workspaceSentinelContents = "GUI for CLI bundle workspace\n";

type WorkspaceRootSource = "environment" | "settings" | "default";

/** App-wide settings. They live beside the app's data, not in the workspace, since they say where the workspace is. */
export function appSettingsPath() {
    return path.join(appSupportDirectory(), "settings.json");
}

/** Where a bundle that declares no defaultWorkspace keeps its workspace, and where every bundle's used to be. */
export function appDataWorkspacesDirectory() {
    return path.join(appSupportDirectory(), "BundleWorkspaces");
}

/** A bundle may only suggest one visible folder directly in the user's home; it never picks an arbitrary path. */
export function defaultWorkspaceRoot(manifest: Pick<BundleManifest, "id" | "defaultWorkspace">) {
    const declared = manifest.defaultWorkspace?.trim();
    if (!declared) {
        return workspaceRootIn(appDataWorkspacesDirectory(), manifest.id);
    }
    const match = /^~\/([A-Za-z0-9_][A-Za-z0-9_.-]*)$/.exec(declared);
    if (!match?.[1]) {
        throw new Error(`defaultWorkspace must name one visible folder in the home folder, like ~/my-app: ${declared}`);
    }
    return path.join(homedir(), match[1]);
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

/** The bundle's workspace: in the environment override's folder, then the saved folder, then the default. */
export async function resolveWorkspaceRoot(manifest: Pick<BundleManifest, "id" | "defaultWorkspace">): Promise<{ root: string; source: WorkspaceRootSource }> {
    const fromEnvironment = process.env[workspacesDirectoryEnvName]?.trim();
    if (fromEnvironment) {
        return { root: workspaceRootIn(path.resolve(fromEnvironment), manifest.id), source: "environment" };
    }
    const configured = await configuredWorkspacesDirectory();
    return configured
        ? { root: workspaceRootIn(configured, manifest.id), source: "settings" }
        : { root: defaultWorkspaceRoot(manifest), source: "default" };
}

export function workspaceRootIn(directory: string, bundleID: unknown) {
    return path.join(directory, safePathComponent(bundleID));
}

/**
 * Syncing replaces entries named like the bundle's, so a folder may only hold this bundle's workspace
 * if it is empty or already a workspace; someone else's folder of that name is never adopted.
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

export async function workspaceLocationInfo(manifest: BundleManifest, workspaceRoot: string): Promise<WorkspaceLocationInfo> {
    const defaultRoot = defaultWorkspaceRoot(manifest);
    return {
        workspaceRoot,
        defaultDirectory: path.dirname(defaultRoot),
        configuredDirectory: await configuredWorkspacesDirectory(),
        environmentOverride: (await resolveWorkspaceRoot(manifest)).source === "environment",
        usingDefault: path.relative(defaultRoot, workspaceRoot) === "",
    };
}

/** Records the folder holding the workspace, or clears it when null. Moving the workspace there is workspace-move's. */
export async function saveWorkspacesDirectory(directory: string | null) {
    const settings = await readAppSettings();
    if (directory == null) {
        delete settings.bundleWorkspacesDirectory;
    }
    else {
        settings.bundleWorkspacesDirectory = directory;
    }
    await mkdir(appSupportDirectory(), { recursive: true });
    await writeFile(appSettingsPath(), `${JSON.stringify(settings, null, 2)}\n`, "utf8");
}
