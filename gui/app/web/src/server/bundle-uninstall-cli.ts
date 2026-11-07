#!/usr/bin/env node
/**
 * Runs a bundle's uninstall steps on the app's workspace for it, without the app's window: the
 * Windows uninstaller and the Linux app's Uninstall run this so that uninstalling the app also
 * removes what its setup installed (the same steps as /api/uninstall/stream). Prints the steps'
 * output; exits 1 if any step failed.
 *
 * Usage: bundle-uninstall-cli.js --app-support-name <app identifier> [--print-workspace] [--remove-workspace] [--remove-app-data] <bundle>
 * A workspace left at the old default location, inside the app's data folder, is always removed: the
 * user can't be expected to know about it. Unless a step failed, --remove-workspace then deletes the
 * bundle's workspace with its libraries, settings and outputs (ask the user first; --print-workspace
 * only prints its path, or nothing if there is none), and --remove-app-data the app's data folder.
 * GUI_FOR_CLI_UNINSTALL_RESULT_FILE, if set, receives the result as JSON (as from the macOS app).
 */
import { existsSync } from "node:fs";
import { rm, rmdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { loadManifestFromRoot } from "./bundle-loader.js";
import { errorMessage } from "./errors.js";
import { appSupportDirectory } from "./paths.js";
import { createProcessManager } from "./process-runner.js";
import { runUninstall, type SetupSummary } from "./setup-runner.js";
import { appDataWorkspacesDirectory, isManagedWorkspace, resolveWorkspaceRoot, workspaceRootIn } from "./workspace-location.js";

type UninstallResult = SetupSummary & { log: string[] };

function parseArgs(argv: string[]) {
    const args = [...argv];
    const take = (flag: string) => {
        const index = args.indexOf(flag);
        return index < 0 ? false : Boolean(args.splice(index, 1));
    };
    const nameIndex = args.indexOf("--app-support-name");
    const appSupportName = nameIndex >= 0 ? args.splice(nameIndex, 2)[1] : undefined;
    const printWorkspace = take("--print-workspace");
    const removeWorkspace = take("--remove-workspace");
    const removeAppData = take("--remove-app-data");
    const [bundle, ...rest] = args;
    if (!appSupportName || !bundle || rest.length > 0) {
        throw new Error("Usage: bundle-uninstall-cli.js --app-support-name <app identifier> [--print-workspace] [--remove-workspace] [--remove-app-data] <bundle>");
    }
    return { appSupportName, printWorkspace, removeWorkspace, removeAppData, bundle };
}

async function main(argv: string[]): Promise<number> {
    const { appSupportName, printWorkspace, removeWorkspace, removeAppData, bundle } = parseArgs(argv);
    process.env.GUI_FOR_CLI_APP_SUPPORT_NAME = appSupportName;
    const manifest = await loadManifestFromRoot(path.resolve(bundle));
    const appData = appSupportDirectory();
    // Where prepareBundleWorkspace keeps the bundle's workspace; it can be outside the app's data folder.
    const workspace = (await resolveWorkspaceRoot(manifest)).root;
    if (printWorkspace) {
        if (await isManagedWorkspace(workspace)) {
            console.log(workspace);
        }
        return 0;
    }
    const oldWorkspace = workspaceRootIn(appDataWorkspacesDirectory(), manifest.id);
    const hasOldWorkspace = path.relative(oldWorkspace, workspace) !== "" && existsSync(oldWorkspace);
    const result: UninstallResult = { status: "ok", results: [], log: [] };
    const note = (line: string) => {
        console.log(line);
        result.log.push(line);
    };
    if (!existsSync(workspace)) {
        note(`No workspace at ${workspace}; setup never ran, nothing to remove.`);
    }
    else {
        Object.assign(result, await runWorkspaceSteps(workspace));
    }
    if (hasOldWorkspace && result.status === "ok") {
        Object.assign(result, await runWorkspaceSteps(oldWorkspace));
    }
    if (result.status !== "ok") {
        if (hasOldWorkspace || removeWorkspace || removeAppData) {
            // Kept so the user can retry or read what failed.
            note(`Uninstall steps failed; kept ${removeAppData ? appData : workspace}`);
        }
    }
    else {
        if (hasOldWorkspace) {
            await rm(oldWorkspace, { recursive: true, force: true });
            await rmdir(path.dirname(oldWorkspace)).catch(() => undefined);
            note(`Removed ${oldWorkspace}`);
        }
        if (removeWorkspace && await isManagedWorkspace(workspace)) {
            await rm(workspace, { recursive: true, force: true });
            note(`Removed ${workspace}`);
        }
        if (removeAppData) {
            await rm(appData, { recursive: true, force: true });
            note(`Removed ${appData}`);
        }
    }
    await writeResult(result);
    return result.status === "ok" ? 0 : 1;
}

/** Runs the steps from the workspace's own copy of the bundle: the scripts setup ran. */
async function runWorkspaceSteps(workspace: string) {
    const processManager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });
    return runUninstall(await loadManifestFromRoot(workspace), workspace, processManager.runProcess, (event) => {
        if (event.type === "step-start") {
            console.log(`==> ${event.step.label ?? event.step.id}`);
        }
        else if (event.type === "output") {
            process.stdout.write(event.text);
        }
        else if (event.type === "step-complete") {
            console.log(`<== ${event.result.id}: ${event.result.status} (exit code ${event.result.exitCode})`);
        }
    });
}

async function writeResult(result: UninstallResult) {
    const resultFile = process.env.GUI_FOR_CLI_UNINSTALL_RESULT_FILE;
    if (resultFile) {
        await writeFile(resultFile, `${JSON.stringify(result, null, 2)}\n`, "utf8");
    }
}

main(process.argv.slice(2)).then(
    (code) => {
        process.exitCode = code;
    },
    async (error) => {
        console.error(`Uninstall failed: ${errorMessage(error)}`);
        process.exitCode = 1;
        await writeResult({ status: "failed", results: [], error: errorMessage(error), log: [] });
    },
);
