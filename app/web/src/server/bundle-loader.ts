import type { Stats } from "node:fs";
import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import { mergeIconMaps, parseIconMapToml } from "../../../shared/icon-map.js";
import { parseJsonWithComments } from "../../../shared/json-comments.js";
import { localizeManifest, localizationLabels, mergeTables, parseTomlStrings, parseTomlStringValue } from "../../../shared/localization.js";
import type { BundleManifest, BundlePage, ExitCodeReference, LocalizedOption, SetupStep, StringMap } from "../../../shared/types.js";
import { bootstrapConfigFiles, initialCheckedOptions, initialConfigFilePaths, initialConfigValues, initialFieldValues, loadBundleState, takeBundleStateWarning, } from "./config-store.js";
import { isSafePageFileName } from "./paths.js";
import { validatePlatformScriptSets } from "./platform-scripts.js";
import { validateManifest } from "./manifest-validator.js";
import { bundleArchiveFormat, extractBundleArchive } from "./bundle-archive.js";
import { errnoCode, errorMessage } from "./errors.js";

const CHOOSE_BUNDLE_MESSAGE = "Choose a bundle folder, manifest.json file, or bundle archive (.zip, .tar, .tar.gz, .tgz).";

export async function resolveBundleSourceRoot(source: string): Promise<string> {
    const value = String(source ?? "").trim();
    if (!value) {
        throw new Error(CHOOSE_BUNDLE_MESSAGE);
    }
    const resolved = path.resolve(value);
    let info: Stats;
    try {
        info = await stat(resolved);
    }
    catch {
        throw new Error(CHOOSE_BUNDLE_MESSAGE);
    }
    if (info.isDirectory()) {
        return resolved;
    }
    if (info.isFile() && path.basename(resolved).toLowerCase() === "manifest.json") {
        return path.dirname(resolved);
    }
    const archiveFormat = info.isFile() ? bundleArchiveFormat(resolved) : null;
    if (archiveFormat != null) {
        return extractBundleArchive(resolved, archiveFormat);
    }
    throw new Error(CHOOSE_BUNDLE_MESSAGE);
}

export async function loadManifestFromRoot(root: string): Promise<BundleManifest> {
    const manifestPath = path.join(root, "manifest.json");
    const manifest = parseJsonWithComments<BundleManifest>(await readFile(manifestPath, "utf8"));
    const pageFiles: unknown[] = Array.isArray(manifest.pages) ? manifest.pages : [];
    if (Array.isArray(manifest.pages) && pageFiles.every((page): page is string => typeof page === "string")) {
        manifest.pageFiles = pageFiles;
        manifest.pages = await Promise.all(pageFiles.map(async (pageFile) => {
            if (!isSafePageFileName(pageFile)) {
                throw new Error(`Invalid page file name: ${pageFile}`);
            }
            return parseJsonWithComments<BundlePage>(await readFile(path.join(root, "pages", pageFile), "utf8"));
        }));
    }
    manifest.setup = manifest.setup ?? { steps: [] };
    manifest.uninstall = manifest.uninstall ?? { steps: [] };
    manifest.exitCodeReference = manifest.exitCodeReference ?? [];
    manifest.defaultLocalizationCode = manifest.defaultLocalizationCode ?? "en";
    validateManifest(manifest);
    await resolveSetupToolVersions(manifest, root);
    await validatePlatformScriptSets(root, manifest);
    return manifest;
}
export async function loadLocaleOptions(repoRoot: string, bundleRoot: string, rawManifest?: BundleManifest): Promise<{ defaultLocalizationCode: string; options: LocalizedOption[] }> {
    const manifest = rawManifest ?? (await loadManifestFromRoot(bundleRoot));
    const defaultCode = manifest.defaultLocalizationCode ?? "en";
    const seen = new Map<string, LocalizedOption>();
    const builtinOptions = await Promise.all((await availableBuiltinLocaleCodes(repoRoot)).map(async (code) => {
        const displayName = await readLanguageDisplayName(path.join(builtinStringsRoot(repoRoot), `strings.${code}.toml`));
        return { code, displayName: displayName ?? code, isAITranslated: false };
    }));
    for (const option of builtinOptions) {
        seen.set(option.code, option);
    }
    const bundleOptions = await Promise.all((await availableBundleLocaleCodes(bundleRoot)).map(async (code) => {
        const filePath = path.join(bundleRoot, "strings", `strings.${code}.toml`);
        const displayName = await readLanguageDisplayName(filePath);
        const isAITranslated = await readLanguageAITranslatedFlag(filePath);
        return {
            code,
            displayName: displayName ?? seen.get(code)?.displayName ?? code,
            isAITranslated: isAITranslated ?? seen.get(code)?.isAITranslated ?? false,
        };
    }));
    for (const option of bundleOptions) {
        seen.set(option.code, option);
    }
    const options = [...seen.values()].sort((first, second) => {
        if (first.code === defaultCode)
            return -1;
        if (second.code === defaultCode)
            return 1;
        return first.displayName.localeCompare(second.displayName);
    });
    return { defaultLocalizationCode: defaultCode, options };
}
export async function loadStringTable(manifest: BundleManifest, locale: string, repoRoot: string, bundleRoot: string): Promise<StringMap> {
    const defaultCode = manifest.defaultLocalizationCode ?? "en";
    const builtinBase = await readBuiltinTable("en", repoRoot);
    const builtinOverlay = locale === "en" ? {} : await readBuiltinTable(locale, repoRoot);
    const bundleBase = await readBundleTable(defaultCode, bundleRoot);
    const bundleOverlay = locale === defaultCode ? {} : await readBundleTable(locale, bundleRoot);
    return mergeTables(builtinBase, builtinOverlay, bundleBase, bundleOverlay);
}
export async function loadIconMap(repoRoot: string, bundleRoot: string) {
    const builtin = await readOptionalIconMap(path.join(builtinResourcesRoot(repoRoot), "BuiltinIconMap", "iconmap.toml"));
    const bundle = await readOptionalIconMap(path.join(bundleRoot, "iconmap.toml"));
    return mergeIconMaps(builtin, bundle);
}
type ExitCodeReferenceEntry = ExitCodeReference & { code: number; severity: string };
export function effectiveExitCodeReference(overrides: ExitCodeReference[] = [], table: StringMap = {}): ExitCodeReferenceEntry[] {
    const defaults: ExitCodeReferenceEntry[] = [
        {
            code: 1,
            title: table["exitCodes.default.1.title"] ?? "General command failure",
            summary: table["exitCodes.default.1.summary"] ??
                "The command reported a generic failure. Review the output for details.",
            severity: "error",
        },
        {
            code: 2,
            title: table["exitCodes.default.2.title"] ?? "Command-line usage error",
            summary: table["exitCodes.default.2.summary"] ??
                "The command arguments were not accepted. Check required inputs, paths, and selected options before running again.",
            severity: "error",
        },
        {
            code: 126,
            title: table["exitCodes.default.126.title"] ?? "Command found but not executable",
            summary: table["exitCodes.default.126.summary"] ??
                "The command or script exists but could not be executed. Check file permissions and whether setup completed successfully.",
            severity: "error",
        },
        {
            code: 127,
            title: table["exitCodes.default.127.title"] ?? "Command not found",
            summary: table["exitCodes.default.127.summary"] ??
                "The command runner could not find the executable. Run setup for this bundle and verify the runtime workspace exists.",
            severity: "error",
        },
        {
            code: 130,
            title: table["exitCodes.default.130.title"] ?? "Command cancelled",
            summary: table["exitCodes.default.130.summary"] ??
                "The command was interrupted by the user. Any partial output or temporary files may need to be cleaned up before retrying.",
            severity: "warning",
        },
    ];
    const byCode = new Map<number, ExitCodeReferenceEntry>(defaults.map((entry) => [entry.code, entry]));
    for (const entry of overrides) {
        const code = Number(entry.code);
        byCode.set(code, { ...entry, code, severity: entry.severity ?? "error" });
    }
    return [...byCode.values()].sort((first, second) => first.code - second.code);
}
async function readBuiltinTable(code: string, repoRoot: string) {
    return readOptionalTable(path.join(builtinStringsRoot(repoRoot), `strings.${code}.toml`));
}
async function readBundleTable(code: string, bundleRoot: string) {
    return readOptionalTable(path.join(bundleRoot, "strings", `strings.${code}.toml`));
}
async function readOptionalTable(filePath: string): Promise<StringMap> {
    try {
        return parseTomlStrings(await readFile(filePath, "utf8"));
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT") {
            return {};
        }
        throw error;
    }
}
async function readOptionalIconMap(filePath: string) {
    try {
        return parseIconMapToml(await readFile(filePath, "utf8"));
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT") {
            return {};
        }
        throw error;
    }
}
async function availableBuiltinLocaleCodes(repoRoot: string) {
    return availableLocaleCodes(builtinStringsRoot(repoRoot));
}
function builtinStringsRoot(repoRoot: string) {
    return path.join(builtinResourcesRoot(repoRoot), "BuiltinStrings");
}
function builtinResourcesRoot(repoRoot: string) {
    return path.join(repoRoot, "resources");
}
async function availableBundleLocaleCodes(bundleRoot: string) {
    return availableLocaleCodes(path.join(bundleRoot, "strings"));
}
async function availableLocaleCodes(directory: string): Promise<string[]> {
    try {
        const files = await readdir(directory);
        return files
            .map((file) => /^strings\.([A-Za-z0-9_-]+)\.toml$/.exec(file)?.[1])
            .filter((code): code is string => Boolean(code));
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT") {
            return [];
        }
        throw error;
    }
}
async function readLanguageDisplayName(filePath: string) {
    try {
        return parseTomlStringValue(await readFile(filePath, "utf8"), "language.name");
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT") {
            return undefined;
        }
        throw error;
    }
}
async function readLanguageAITranslatedFlag(filePath: string) {
    try {
        const value = parseTomlStringValue(await readFile(filePath, "utf8"), "language.aiTranslated");
        if (value == null) {
            return undefined;
        }
        const normalized = value.trim().toLowerCase();
        if (["true", "yes", "1"].includes(normalized)) {
            return true;
        }
        if (["false", "no", "0"].includes(normalized)) {
            return false;
        }
        return undefined;
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT") {
            return undefined;
        }
        throw error;
    }
}
export async function loadLocalizedBundle(locale: string | undefined, repoRoot: string, bundleRoot: string, sourceBundleRoot: string, preferredLocales: readonly string[] = []) {
    const rawManifest = await loadManifestFromRoot(bundleRoot);
    const locales = await loadLocaleOptions(repoRoot, bundleRoot, rawManifest);
    const bundleState = await loadBundleState(bundleRoot);
    const systemLocale = matchLocalizationCode(preferredLocales, locales.options);
    const effectiveLocale = locale && locales.options.some((option) => option.code === locale)
        ? locale
        : bundleState.localizationCode && locales.options.some((option) => option.code === bundleState.localizationCode)
            ? bundleState.localizationCode
            : systemLocale ?? rawManifest.defaultLocalizationCode ?? "en";
    const table = await loadStringTable(rawManifest, effectiveLocale, repoRoot, bundleRoot);
    const localizedOptions = locales.options.map((option) => ({
        ...option,
        displayName: table[`language.names.${option.code}`] ?? option.displayName,
    }));
    const manifest = localizeManifest(rawManifest, table);
    const iconMap = await loadIconMap(repoRoot, bundleRoot);
    manifest.exitCodeReference = effectiveExitCodeReference(manifest.exitCodeReference, table);
    const configFilePaths = initialConfigFilePaths(manifest, bundleState, bundleRoot);
    await bootstrapConfigFiles(manifest, bundleRoot, configFilePaths);
    const configWarnings: string[] = [];
    const configValues = await initialConfigValues(manifest, configFilePaths, bundleRoot, configWarnings);
    const fieldValues = initialFieldValues(manifest, configValues, bundleState);
    const checkedOptions = initialCheckedOptions(manifest, configValues, bundleState);
    const stateWarning = takeBundleStateWarning(bundleRoot);
    return {
        ...(stateWarning ? { stateWarning } : {}),
        configWarnings,
        manifest,
        labels: localizationLabels(table),
        iconMap,
        localizationCode: effectiveLocale,
        localizationOptions: localizedOptions,
        usingSystemDefaultLocale: !locale && !bundleState.localizationCode,
        bundleRootPath: bundleRoot,
        sourceRootPath: sourceBundleRoot,
        bundleState,
        configFilePaths,
        configValues,
        fieldValues,
        checkedOptions,
    };
}
function matchLocalizationCode(preferences: readonly unknown[], options: readonly LocalizedOption[]) {
    const available = new Set(options.map((option) => option.code));
    for (const raw of preferences ?? []) {
        const candidate = String(raw ?? "").trim();
        if (!candidate) {
            continue;
        }
        if (available.has(candidate)) {
            return candidate;
        }
        const [primary, ...rest] = candidate.split("-");
        if (primary && available.has(primary)) {
            return primary;
        }
        if (primary === "zh") {
            const region = rest.join("-").toLowerCase();
            if (["cn", "sg", "hans"].some((part) => region.includes(part)) && available.has("zh-Hans")) {
                return "zh-Hans";
            }
            if (["tw", "hk", "mo", "hant"].some((part) => region.includes(part)) && available.has("zh-Hant")) {
                return "zh-Hant";
            }
        }
    }
    return undefined;
}

export function createOneShotBundlePreload<Bundle>(load: (locale?: string, preferredLocales?: string[]) => Promise<Bundle>, initialLocale: string | undefined, enabled: boolean) {
    let preloadedLocale = enabled ? localeCacheKey(initialLocale) : undefined;
    let preloadedBundle = enabled ? load(initialLocale) : undefined;
    // The first request may not use the preload (another locale), so nothing may ever await it; its
    // failure must not become an unhandled rejection, which ends the server. Requests that do use it
    // still get the error.
    preloadedBundle?.catch(() => undefined);
    return {
        preloaded: preloadedBundle,
        async load(locale?: string, preferredLocales: string[] = []) {
            if (preloadedBundle && preferredLocales.length === 0 && localeCacheKey(locale) === preloadedLocale) {
                const bundle = await preloadedBundle;
                preloadedBundle = undefined;
                preloadedLocale = undefined;
                return bundle;
            }
            return load(locale, preferredLocales);
        },
    };
}

function localeCacheKey(locale?: string) {
    return locale ?? "";
}

async function resolveSetupToolVersions(manifest: BundleManifest, root: string): Promise<void> {
    const stepGroups: Array<[string, SetupStep[]]> = [
        ["setup.steps", manifest.setup?.steps ?? []],
        ["uninstall.steps", manifest.uninstall?.steps ?? []],
    ];
    for (const [scope, steps] of stepGroups) {
        for (const step of steps) {
            if (step.toolVersion || !step.toolVersionFile) {
                continue;
            }
            const stepID = String(step.id ?? "<unknown>");
            const stepLabel = String(step.label ?? "").trim();
            const context = `${scope}.${stepID}${stepLabel ? ` (${stepLabel})` : ""}.toolVersionFile`;
            const versionFile = String(step.toolVersionFile);
            if (!isSafeRelativePath(versionFile)) {
                throw new Error(`Invalid ${context}: ${versionFile}`);
            }
            try {
                const firstLine = (await readFile(path.join(root, versionFile), "utf8")).split(/\r?\n/, 1)[0]?.trim();
                if (firstLine) {
                    step.toolVersion = firstLine;
                }
            }
            catch (error) {
                const message = error instanceof Error ? errorMessage(error) : String(error);
                throw new Error(`Could not read ${context} at ${versionFile}: ${message}`, { cause: error });
            }
        }
    }
}

function isSafeRelativePath(value: string): boolean {
    const normalized = value.replaceAll("\\", "/");
    return Boolean(normalized.trim()) && !path.isAbsolute(normalized) && !normalized.split("/").includes("..");
}
