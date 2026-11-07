import { realpathSync } from "node:fs";
import { homedir, platform } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { checkedOptionsForContext } from "../../../shared/rendering.js";
import type { CommandContext, ControlSpec } from "../../../shared/types.js";
import { errnoCode, HttpError } from "./errors.js";
export function parseArgs(argv: readonly string[]): Record<string, string | undefined> {
    const parsed: Record<string, string | undefined> = {};
    const readValue = (flag: string, index: number) => {
        const next = argv[index];
        if (!next || next.startsWith("--")) {
            throw new Error(`Missing value for ${flag}`);
        }
        return next;
    };
    for (let index = 0; index < argv.length; index += 1) {
        const arg = argv[index];
        if (arg === "--bundle")
            parsed.bundle = readValue("--bundle", ++index);
        else if (arg === "--port")
            parsed.port = readValue("--port", ++index);
        else if (arg === "--host")
            parsed.host = readValue("--host", ++index);
        else if (arg === "--locale")
            parsed.locale = readValue("--locale", ++index);
        else if (arg === "--theme")
            parsed.theme = readValue("--theme", ++index);
        else if (arg === "--once")
            parsed.once = "true";
        else if (arg === "--benchmark")
            parsed.benchmark = "true";
        else if (arg === "--setup")
            parsed.setup = "true";
        else if (arg === "--no-setup")
            parsed.setup = "false";
        else if (arg === "--help" || arg === "-h")
            parsed.help = "true";
        else if (!parsed.bundle)
            parsed.bundle = arg;
    }
    return parsed;
}
export function applicationSupportDirectory() {
    if (platform() === "darwin") {
        return path.join(homedir(), "Library", "Application Support");
    }
    if (platform() === "win32") {
        return process.env.LOCALAPPDATA || path.join(homedir(), "AppData", "Local");
    }
    return process.env.XDG_DATA_HOME || path.join(homedir(), ".local", "share");
}
export function appSupportContainerName() {
    return safePathComponent(process.env.GUI_FOR_CLI_APP_SUPPORT_NAME || "gui-for-cli");
}
export function appSupportDirectory() {
    return path.join(applicationSupportDirectory(), appSupportContainerName());
}
export function safePathComponent(value: unknown): string {
    const sanitized = String(value)
        .split("")
        .map((character) => (/[A-Za-z0-9_.-]/.test(character) ? character : "-"))
        .join("")
        .replace(/^[.-]+|[.-]+$/g, "");
    return sanitized || "bundle";
}
export function environmentKey(value: unknown): string {
    return String(value)
        .split("")
        .map((character) => (/[A-Za-z0-9]/.test(character) ? character.toUpperCase() : "_"))
        .join("");
}
export function isSafePageFileName(value: string | undefined): boolean {
    return Boolean(value &&
        !value.startsWith("/") &&
        !value.startsWith("\\") &&
        !value.includes("/") &&
        !value.includes("\\") &&
        !value.split("/").includes("..") &&
        !value.split("\\").includes("..") &&
        value.endsWith(".json"));
}
export function decodeXML(value: unknown): string {
    return String(value)
        .replaceAll("&lt;", "<")
        .replaceAll("&gt;", ">")
        .replaceAll("&quot;", '"')
        .replaceAll("&apos;", "'")
        .replaceAll("&amp;", "&");
}
export function formatGB(value: number): string {
    if (value >= 100) {
        return value.toFixed(0);
    }
    if (value >= 10) {
        return value.toFixed(1);
    }
    return value.toFixed(2);
}
export function expandPathTokens(value: unknown, bundleRoot: string, configPathValue = ""): string {
    const tokens = pathTokenValues(bundleRoot, configPathValue);
    let expanded = String(value);
    expanded = replacePathToken(expanded, "{{bundleRoot}}", tokens.bundleRoot);
    expanded = replacePathToken(expanded, "{{bundleWorkspace}}", tokens.bundleWorkspace);
    expanded = expanded.replaceAll("{{bundleWorkspaceFileURL}}", tokens.bundleWorkspaceFileURL);
    expanded = expanded.replaceAll("{{bundleRootBasename}}", tokens.bundleRootBasename);
    expanded = replacePathToken(expanded, "{{home}}", tokens.home);
    expanded = replacePathToken(expanded, "{{configHome}}", tokens.configHome);
    expanded = replacePathToken(expanded, "{{userConfig}}", tokens.configHome);
    expanded = replacePathToken(expanded, "{{applicationSupport}}", tokens.applicationSupport);
    expanded = replacePathToken(expanded, "{{appConfig}}", tokens.applicationSupport);
    expanded = expanded.replaceAll("{{configPath}}", tokens.configPath);
    expanded = replacePathToken(expanded, "{{configDir}}", tokens.configDir);
    return expandEnvironmentVariables(expanded.replace(/^~(?=\/|$)/, tokens.home));
}
export function pathTokenValues(bundleRoot: string, configPathValue = "") {
    const home = homedir();
    const configHome = process.env.XDG_CONFIG_HOME || path.join(home, ".config");
    const applicationSupport = applicationSupportDirectory();
    return {
        bundleRoot,
        bundleWorkspace: bundleRoot,
        bundleWorkspaceFileURL: pathToFileURL(bundleRoot).href,
        bundleRootBasename: path.basename(bundleRoot),
        home,
        configHome,
        applicationSupport,
        configPath: configPathValue ?? "",
        configDir: configPathValue ? path.dirname(configPathValue) : "",
    };
}
export function resolveUserPath(value: unknown, bundleRoot: string): string {
    const expanded = expandPathTokens(value, bundleRoot);
    return path.isAbsolute(expanded) ? expanded : path.resolve(bundleRoot, expanded);
}

function expandEnvironmentVariables(value: string): string {
    return value.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (match, key) => process.env[key] ?? match);
}

function replacePathToken(value: string, token: string, replacement: string): string {
    const expanded = value
        .replace(new RegExp(`${escapeRegExp(token)}[\\\\/]+`, "g"), pathWithTrailingSeparator(replacement))
        .replaceAll(token, replacement);
    return value.includes(token) && path.sep === "\\" ? expanded.replaceAll("/", "\\") : expanded;
}

function pathWithTrailingSeparator(value: string): string {
    return /[\\/]/.test(value.at(-1) ?? "") ? value : `${value}${path.sep}`;
}

function escapeRegExp(value: string): string {
    return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
export function resolveBundlePath(value: string, bundleRoot: string): string {
    const expanded = expandPathTokens(value, bundleRoot);
    return resolvePathWithinRoot(expanded, bundleRoot, value);
}
export function resolvePathWithinRoot(value: string, root: string, originalValue = value): string {
    if (path.isAbsolute(value)) {
        throw new Error(`Bundle script paths must be relative: ${originalValue}`);
    }
    const candidate = path.resolve(root, value);
    if (!isPathInside(candidate, root)) {
        throw new Error(`Bundle script path escapes bundle root: ${originalValue}`);
    }
    const realCandidate = realPathIfExists(candidate);
    if (realCandidate) {
        const realRoot = realpathSync(root);
        if (!isPathInside(realCandidate, realRoot)) {
            throw new Error(`Bundle script path escapes bundle root: ${originalValue}`);
        }
        return realCandidate;
    }
    return candidate;
}
export function isPathInside(candidate: string, root: string): boolean {
    return candidate === root || candidate.startsWith(`${root}${path.sep}`);
}
export function relativeTopLevelName(root: string, candidate: string): string | undefined {
    const relative = path.relative(root, candidate);
    if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
        return undefined;
    }
    return relative.split(path.sep)[0];
}
function realPathIfExists(candidate: string): string | undefined {
    try {
        return realpathSync(candidate);
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT" || errnoCode(error) === "ENOTDIR") {
            return undefined;
        }
        throw error;
    }
}
/**
 * Resolves the settings file a config editor reads and writes. Users may point an editor at another file,
 * but only one with the same extension as the bundle-declared file, so a request can never turn a settings
 * save into a write to an arbitrary file (shell profiles, keys, launch agents, ...).
 */
export function configPath(control: ControlSpec | undefined, requestedPath: string | undefined, bundleRoot: string): string {
    const declaredPath = control?.configFile?.path;
    if (!declaredPath) {
        throw new HttpError(400, `${control?.label ?? control?.id ?? "This settings editor"} does not declare a settings file.`);
    }
    const declared = resolveUserPath(declaredPath, bundleRoot);
    const requested = requestedPath?.trim() ? resolveUserPath(requestedPath, bundleRoot) : declared;
    const extension = path.extname(declared).toLowerCase();
    if (requested !== declared && (!extension || path.extname(requested).toLowerCase() !== extension)) {
        throw new HttpError(400, `Settings file must be a ${extension || "bundle-declared"} file: ${requested}`);
    }
    return requested;
}
export function distModulePath(pathname: string, distRoot: string): string | undefined {
    const match = /^\/(client|shared)\/([A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*\.js)$/.exec(pathname);
    if (!match) {
        return undefined;
    }
    const modulePath = (match[2] ?? "").split("/");
    if (modulePath.some((segment) => segment === "." || segment === "..")) {
        return undefined;
    }
    if (match[1] === "client") {
        return path.join(distRoot, "web", "src", "client", ...modulePath);
    }
    return path.join(distRoot, "shared", ...modulePath);
}
export function normalizeContext(context: CommandContext = {}, bundleRoot: string): CommandContext {
    return {
        ...context,
        fieldValues: context.fieldValues ?? {},
        checkedOptions: context.checkedOptions ?? checkedOptionsForContext({}),
        configValues: context.configValues ?? {},
        rowValues: context.rowValues ?? {},
        bundleRootPath: bundleRoot,
        homePath: homedir(),
    };
}
