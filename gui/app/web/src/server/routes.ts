import type { IncomingMessage, ServerResponse } from "node:http";
import path from "node:path";
import type { BundleManifest, Labels, LooseRecord, RunProcess } from "../../../shared/types.js";
import { evaluatePrecheck, evaluateSetupInstallSizePrecheck, fileStateValues, runAction, runDataSource } from "./action-runner.js";
import { serveBundleFavicon, serveBundleFile } from "./assets.js";
import { loadLocaleOptions } from "./bundle-loader.js";
import { loadConfig, saveBundleState, saveConfig } from "./config-store.js";
import { errorMessage, HttpError } from "./errors.js";
import { contentType, json, notFound, readJSONBody, serveIndex, staticFile } from "./http.js";
import { findConfigEditorControl, locationKey, resolveActionReference, resolveDataSource, type DynamicActions } from "./manifest-lookup.js";
import { streamNDJSON } from "./ndjson.js";
import { configPath, distModulePath, normalizeContext } from "./paths.js";
import { pickPath, type NativePickerEndpoint } from "./path-picker.js";
import { optionalString, parseActionReference, parseBundleStatePatch, parseCommandContext, parseLabels, parseManifestLocation, parsePrecheckTarget, requestObject, requiredString } from "./request-bodies.js";
import { applySecurityHeaders, guardRequest } from "./request-guard.js";
import { runSetup, runUninstall } from "./setup-runner.js";
import { wrapEmitWithEventLog } from "./setup-event-log.js";
import { bundleArchiveFormat } from "./bundle-archive.js";

type ManifestResponse = Record<string, unknown> & { appVersion?: string; autoRunSetup?: boolean };

interface LocalizedBundle {
    manifest: BundleManifest;
    labels?: Labels;
    [key: string]: unknown;
}

export interface RequestHandlerContext {
    addDevReloadClient: (response: ServerResponse) => void;
    apiToken: string;
    /** True when a launcher (Tauri/Electron) injects the token into its own webview instead of index.html. */
    apiTokenInjectedByLauncher?: boolean;
    appVersion?: string;
    bundleRoot: string;
    defaultLocale?: string;
    distRoot: string;
    /** Actions most recently returned by control data sources, which replace the control's declared row actions. */
    dynamicActions: DynamicActions;
    enableDevReload: boolean;
    hostnames?: string[];
    localizedBundleLoader: {
        load: (locale?: string, preferredLocales?: string[]) => Promise<LocalizedBundle>;
    };
    loadBundle: (requestedSource: string) => Promise<LooseRecord>;
    /** The workspace manifest; actions, data sources, and settings files are only ever resolved from it. */
    manifest: BundleManifest;
    maxBodyBytes: number;
    nativePicker?: NativePickerEndpoint | undefined;
    repoRoot: string;
    runProcess: RunProcess;
    shutdown: (reason: string) => void;
    sourceBundleRoot: string;
    webRoot: string;
}

type RouteHandler = (response: ServerResponse, headOnly: boolean) => Promise<void>;
type JSONHandler = (body: LooseRecord) => Promise<unknown>;
type StepRunner = typeof runSetup;

const maxRememberedPicks = 16;

export function createRequestHandler(context: RequestHandlerContext) {
    // Bundles can only be loaded from folders the user picked in a native dialog during this session.
    const pickedBundlePaths = new Set<string>();
    const staticRoutes: Record<string, RouteHandler> = {
        "/": (response, headOnly) => serveIndex(path.join(context.webRoot, "index.html"), indexToken(context), response, headOnly),
        "/index.html": (response, headOnly) => serveIndex(path.join(context.webRoot, "index.html"), indexToken(context), response, headOnly),
        "/favicon.ico": (response, headOnly) => serveBundleFavicon(response, headOnly, context.bundleRoot),
        "/client/app.js": (response, headOnly) => staticFile(path.join(context.distRoot, "web", "src", "client", "app.js"), "text/javascript; charset=utf-8", response, headOnly),
        "/styles.css": (response, headOnly) => staticFile(path.join(context.webRoot, "styles.css"), "text/css; charset=utf-8", response, headOnly),
    };
    const postRoutes: Record<string, (request: IncomingMessage, response: ServerResponse) => Promise<void>> = {
        "/api/datasource": jsonRoute(context, async (body) => {
            const location = parseManifestLocation(body.source);
            const payload = await runDataSource(resolveDataSource(context.manifest, location), commandContext(body, context), context.bundleRoot, context.runProcess);
            rememberDynamicActions(context, location, payload);
            return payload;
        }),
        "/api/run/stream": async (request, response) => {
            const body = await readBody(request, context);
            const action = resolveActionReference(context.manifest, parseActionReference(body.action), context.dynamicActions);
            const actionContext = commandContext(body, context);
            await streamNDJSON(
                response,
                (emit, signal) => runAction(action, actionContext, signal, context.bundleRoot, context.runProcess, emit),
                (error) => ({ type: "error", error }),
            );
        },
        "/api/precheck": jsonRoute(context, async (body) => {
            const target = parsePrecheckTarget(body.target);
            const labels = parseLabels(body.labels);
            if (target.kind === "setup") {
                return evaluateSetupInstallSizePrecheck(context.manifest, labels, context.bundleRoot, context.runProcess);
            }
            const action = resolveActionReference(context.manifest, target, context.dynamicActions);
            return evaluatePrecheck(action.precheck, commandContext(body, context), labels, context.bundleRoot, context.runProcess);
        }),
        "/api/file-state": jsonRoute(context, async (body) => ({ values: await fileStateValues(commandContext(body, context), context.bundleRoot) })),
        "/api/setup/stream": (request, response) => handleStepStream(request, response, context, runSetup),
        "/api/uninstall/stream": (request, response) => handleStepStream(request, response, context, runUninstall),
        "/api/path/pick": jsonRoute(context, async (body) => {
            const result = await pickPath({ kind: body.kind, title: body.title, defaultPath: body.defaultPath, bundleRoot: context.bundleRoot, nativePicker: context.nativePicker });
            if (!result.cancelled && (result.kind === "directory" || path.basename(result.path).toLowerCase() === "manifest.json" || bundleArchiveFormat(result.path) != null)) {
                pickedBundlePaths.add(result.path);
                for (const stale of [...pickedBundlePaths].slice(0, -maxRememberedPicks)) {
                    pickedBundlePaths.delete(stale);
                }
            }
            return result;
        }),
        "/api/bundle/load": jsonRoute(context, async (body) => {
            const requested = requiredString(body, "path");
            if (!pickedBundlePaths.delete(requested)) {
                throw new HttpError(403, "Bundles can only be loaded from a path chosen in the file dialog.");
            }
            return context.loadBundle(requested);
        }),
        "/api/shutdown": jsonRoute(context, async () => {
            setTimeout(() => context.shutdown("apiShutdown"), 0).unref();
            return { ok: true };
        }),
        "/api/open-bundle-workspace": jsonRoute(context, async () => {
            await openPath(context.bundleRoot, context.runProcess);
            return { ok: true };
        }),
        "/api/config/load": jsonRoute(context, async (body) => {
            const control = findConfigEditorControl(context.manifest, requiredString(body, "controlID"));
            return loadConfig(control, optionalString(body, "path"), context.bundleRoot);
        }),
        "/api/config/save": jsonRoute(context, async (body) => {
            const control = findConfigEditorControl(context.manifest, requiredString(body, "controlID"));
            return saveConfig(control, optionalString(body, "path"), requestObject(body.values ?? {}, "values"), context.bundleRoot);
        }),
        "/api/state/save": jsonRoute(context, async (body) => {
            const patch = parseBundleStatePatch(body.state);
            for (const [controlID, requestedPath] of Object.entries(patch.configFilePaths ?? {})) {
                configPath(findConfigEditorControl(context.manifest, controlID), requestedPath, context.bundleRoot);
            }
            return saveBundleState(patch, context.bundleRoot);
        }),
    };
    return async (request: IncomingMessage, response: ServerResponse) => {
        applySecurityHeaders(response);
        try {
            const url = new URL(request.url ?? "/", "http://localhost");
            guardRequest(request, url.pathname, { apiToken: context.apiToken, hostnames: context.hostnames ?? [] });
            const method = request.method ?? "GET";
            const headOnly = method === "HEAD";
            if (method === "GET" || method === "HEAD") {
                await handleGet(url, request, response, context, staticRoutes[url.pathname], headOnly);
                return;
            }
            const postRoute = postRoutes[url.pathname];
            if (method === "POST" && postRoute) {
                await postRoute(request, response);
                return;
            }
            throw new HttpError(url.pathname in postRoutes ? 405 : 404, "Not found");
        }
        catch (error) {
            if (response.destroyed) {
                return;
            }
            if (response.headersSent) {
                if (!response.writableEnded) {
                    response.end();
                }
                return;
            }
            json(response, { error: errorMessage(error) }, error instanceof HttpError ? error.status : 500);
        }
    };
}

async function handleGet(url: URL, request: IncomingMessage, response: ServerResponse, context: RequestHandlerContext, staticRoute: RouteHandler | undefined, headOnly: boolean) {
    if (staticRoute) {
        await staticRoute(response, headOnly);
        return;
    }
    if (url.pathname === "/dev/reload" && context.enableDevReload) {
        context.addDevReloadClient(response);
        return;
    }
    if (url.pathname === "/bundle-file") {
        await serveBundleFile(response, url.searchParams.get("path") ?? "", context.bundleRoot, headOnly);
        return;
    }
    const assetPath = webuiVendorAssetPath(url.pathname, context.webRoot) ?? distModulePath(url.pathname, context.distRoot);
    if (assetPath) {
        await staticFile(assetPath, assetPath.endsWith(".js") ? "text/javascript; charset=utf-8" : contentType(assetPath), response, headOnly);
        return;
    }
    if (url.pathname === "/api/locales") {
        json(response, await loadLocaleOptions(context.repoRoot, context.bundleRoot));
        return;
    }
    if (url.pathname === "/api/manifest") {
        const locale = url.searchParams.has("locale") ? url.searchParams.get("locale") || undefined : context.defaultLocale;
        const loaded = await context.localizedBundleLoader.load(locale, preferredLocales(request.headers["accept-language"]));
        const body = withAutoRunSetup(withAppVersion(loaded, context.appVersion));
        json(response, context.enableDevReload ? { ...body, devReload: true } : body);
        return;
    }
    if (url.pathname.startsWith("/api/")) {
        throw new HttpError(404, "Not found");
    }
    notFound(response);
}

function indexToken(context: RequestHandlerContext) {
    return context.apiTokenInjectedByLauncher ? undefined : context.apiToken;
}

function jsonRoute(context: RequestHandlerContext, handler: JSONHandler) {
    return async (request: IncomingMessage, response: ServerResponse) => {
        json(response, await handler(await readBody(request, context)));
    };
}

async function readBody(request: IncomingMessage, context: RequestHandlerContext) {
    return requestObject(await readJSONBody(request, context.maxBodyBytes));
}

function commandContext(body: LooseRecord, context: RequestHandlerContext) {
    return normalizeContext(parseCommandContext(body.context), context.bundleRoot);
}

function rememberDynamicActions(context: RequestHandlerContext, location: ReturnType<typeof parseManifestLocation>, payload: unknown) {
    if (!location.controlID || location.settingID) {
        return;
    }
    const record = payload != null && typeof payload === "object" ? payload as LooseRecord : {};
    const actions = record.rowActions ?? record.actions;
    const key = locationKey(location);
    if (Array.isArray(actions)) {
        context.dynamicActions.set(key, actions);
    }
    else {
        context.dynamicActions.delete(key);
    }
}

async function handleStepStream(request: IncomingMessage, response: ServerResponse, context: RequestHandlerContext, runner: StepRunner) {
    const body = await readBody(request, context);
    const bundle = await context.localizedBundleLoader.load(requestedLocale(body, context.defaultLocale), preferredLocales(request.headers["accept-language"]));
    await streamNDJSON(
        response,
        (emit, signal) => runner(bundle.manifest, context.bundleRoot, context.runProcess, wrapEmitWithEventLog(emit), bundle.labels ?? {}, signal),
        (error) => ({ type: "complete", result: { status: "failed", results: [], error } }),
    );
}

function withAppVersion(bundle: ManifestResponse, appVersion: unknown): ManifestResponse {
    const version = String(appVersion ?? "").trim();
    return version ? { ...bundle, appVersion: version } : bundle;
}

function withAutoRunSetup(bundle: ManifestResponse): ManifestResponse {
    const raw = String(process.env.GUI_FOR_CLI_AUTO_RUN_SETUP ?? "").trim().toLowerCase();
    return ["1", "true", "yes", "on"].includes(raw) ? { ...bundle, autoRunSetup: true } : bundle;
}

function preferredLocales(header: string | string[] | undefined) {
    return String(Array.isArray(header) ? header.join(",") : header ?? "")
        .split(",")
        .map((entry, index) => {
            const [tag = "", ...params] = entry.split(";").map((part) => part.trim());
            const qParam = params.find((part) => part.toLowerCase().startsWith("q="));
            const q = qParam ? Number(qParam.slice(2)) : 1;
            return { tag, q: Number.isFinite(q) ? q : 0, index };
        })
        .filter(({ tag, q }) => Boolean(tag) && tag !== "*" && q > 0)
        .sort((left, right) => right.q - left.q || left.index - right.index)
        .map(({ tag }) => tag);
}

function requestedLocale(body: LooseRecord, defaultLocale: string | undefined) {
    return Object.hasOwn(body, "locale") ? String(body.locale || "") || undefined : defaultLocale;
}

function webuiVendorAssetPath(pathname: string, webRoot: string) {
    const prefix = "/vendor/bootstrap-icons/";
    if (!pathname.startsWith(prefix)) {
        return undefined;
    }
    const vendorRoot = path.resolve(webRoot, "vendor", "bootstrap-icons");
    const filePath = path.resolve(vendorRoot, pathname.slice(prefix.length));
    return filePath.startsWith(`${vendorRoot}${path.sep}`) ? filePath : undefined;
}

async function openPath(filePath: string, runProcess: RunProcess) {
    const opener = process.platform === "win32" ? "explorer.exe" : process.platform === "darwin" ? "/usr/bin/open" : "xdg-open";
    await runProcess(opener, [filePath], { cwd: filePath, env: process.env });
}
