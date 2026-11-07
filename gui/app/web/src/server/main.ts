#!/usr/bin/env node
import { writeFile } from "node:fs/promises";
import { createServer, type Server } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { RequestHandlerContext } from "./routes.js";
import { createOneShotBundlePreload, loadLocalizedBundle, loadManifestFromRoot, resolveBundleSourceRoot } from "./bundle-loader.js";
import { createDevReload } from "./dev-reload.js";
import { createJobServer } from "./job-server.js";
import { createShutdownController } from "./lifecycle.js";
import { parseArgs } from "./paths.js";
import { takeNativePickerEndpoint } from "./path-picker.js";
import { createProcessManager } from "./process-runner.js";
import { takeApiToken } from "./request-guard.js";
import { createRequestHandler } from "./routes.js";
import { prepareBundleWorkspace } from "./workspace.js";
import { moveWorkspace } from "./workspace-move.js";
import { errorMessage } from "./errors.js";

const serverDir = path.dirname(fileURLToPath(import.meta.url));
const distRoot = path.resolve(serverDir, "../../..");
const packageRoot = path.resolve(distRoot, "..");
const webRoot = path.join(packageRoot, "web");
const repoRoot = path.resolve(packageRoot, "..");
const args = parseArgs(process.argv.slice(2));
const sourceBundleRoot = path.resolve(args.bundle ?? path.join(repoRoot, "examples", "WGSExtract"));
const port = Number(args.port ?? process.env.PORT ?? 8787);
const host = args.host ?? "127.0.0.1";
const defaultLocale = args.locale;
const appVersion = process.env.GUI_FOR_CLI_APPLICATION_VERSION?.trim() || "";
const enableDevReload = process.env.WEBUI_DEV_RELOAD === "1";
const launcherProvidedToken = Boolean(process.env.GFC_API_TOKEN);
const apiToken = takeApiToken();
const nativePicker = takeNativePickerEndpoint(apiToken);
const portFile = process.env.GFC_PORT_FILE;
delete process.env.GFC_PORT_FILE;

const { runProcess, terminateAllProcesses } = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });
let bundleRuntime = await loadBundleRuntime(sourceBundleRoot, Boolean(args.bundle));
let server: Server | undefined;
let bundleChangeInFlight = false;
/** Loading a bundle and moving its workspace both replace the running bundle, so only one may run at a time. */
async function exclusively<Result>(change: () => Promise<Result>): Promise<Result> {
    if (bundleChangeInFlight) {
        throw new Error("Bundle load already in progress.");
    }
    bundleChangeInFlight = true;
    try {
        return await change();
    }
    finally {
        bundleChangeInFlight = false;
    }
}
const shutdownController = createShutdownController({
    getServer: () => server,
    terminateAllProcesses,
});
const devReload = createDevReload({ enabled: enableDevReload, distRoot, webRoot });
const requestContext: RequestHandlerContext = {
    addDevReloadClient: devReload.addClient,
    apiToken,
    apiTokenInjectedByLauncher: launcherProvidedToken,
    appVersion,
    ...bundleRuntime,
    ...(defaultLocale != null ? { defaultLocale } : {}),
    distRoot,
    enableDevReload,
    hostnames: ["0.0.0.0", "::", "127.0.0.1", "localhost"].includes(host) ? [] : [host],
    maxBodyBytes: 1_048_576,
    nativePicker,
    repoRoot,
    runProcess,
    shutdown: shutdownController.shutdown,
    webRoot,
    loadBundle: (requestedSource) => exclusively(async () => {
        const nextRuntime = await loadBundleRuntime(requestedSource, false);
        await terminateAllProcesses();
        useBundleRuntime(nextRuntime);
        return {
            bundleRootPath: nextRuntime.bundleRoot,
            sourceRootPath: nextRuntime.sourceBundleRoot,
        };
    }),
    moveWorkspace: (directory, emit, signal) => exclusively(async () => {
        await terminateAllProcesses();
        await moveWorkspace({
            manifest: bundleRuntime.manifest,
            sourceRoot: bundleRuntime.sourceBundleRoot,
            workspaceRoot: bundleRuntime.bundleRoot,
            directory,
            onProgress: (percent) => emit({ type: "progress", percent }),
            signal,
        });
        useBundleRuntime(await loadBundleRuntime(bundleRuntime.sourceBundleRoot, false));
    }),
};

function useBundleRuntime(nextRuntime: Awaited<ReturnType<typeof loadBundleRuntime>>) {
    bundleRuntime = nextRuntime;
    Object.assign(requestContext, nextRuntime);
}

shutdownController.installParentMonitor();
shutdownController.installShutdownHandlers();

const listeningServer = createServer(createRequestHandler(requestContext));
server = listeningServer;
listeningServer.listen(port, host, () => {
    const address = listeningServer.address();
    const boundPort = typeof address === "object" && address ? address.port : port;
    console.log(`GUI for CLI Web UI: http://${host}:${boundPort}/`);
    console.log(`Bundle source: ${bundleRuntime.sourceBundleRoot}`);
    console.log(`Bundle workspace: ${bundleRuntime.bundleRoot}`);
    if (portFile) {
        writeFile(portFile, `${boundPort}\n`).catch((error) => {
            console.error(`Could not write GFC_PORT_FILE: ${errorMessage(error)}`);
            shutdownController.shutdown("portFileError");
        });
    }
});

devReload.installWatcher();

async function loadBundleRuntime(requestedSource: string, preload: boolean) {
    const resolvedSourceBundleRoot = await resolveBundleSourceRoot(requestedSource);
    const sourceManifest = await loadManifestFromRoot(resolvedSourceBundleRoot);
    const resolvedBundleRoot = await prepareBundleWorkspace(sourceManifest, resolvedSourceBundleRoot);
    const manifest = await loadManifestFromRoot(resolvedBundleRoot);
    return {
        sourceBundleRoot: resolvedSourceBundleRoot,
        bundleRoot: resolvedBundleRoot,
        manifest,
        jobServer: manifest.jobServer ? createJobServer(manifest.jobServer, resolvedBundleRoot, runProcess) : undefined,
        dynamicActions: new Map(),
        localizedBundleLoader: createOneShotBundlePreload(
            (locale, preferredLocales = []) => loadLocalizedBundle(locale, repoRoot, resolvedBundleRoot, resolvedSourceBundleRoot, preferredLocales),
            defaultLocale,
            preload,
        ),
    };
}
