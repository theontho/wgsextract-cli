import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { mkdir, readFile, realpath, rename, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { allControls, configEditorControls, parseFlatToml, persistsFieldValue, serializeFlatToml } from "../../../shared/rendering.js";
import type { BundleManifest, ConfigBootstrapScriptSpec, ConfigSetting, ControlSpec, LooseRecord, SetupRun, StateValue, StringMap, ValueMap } from "../../../shared/types.js";
import { configPath, expandPathTokens, expandWorkspaceToken } from "./paths.js";
import { resolvePlatformScriptPath } from "./platform-scripts.js";
import { errnoCode, errorMessage } from "./errors.js";

const bootstrapScriptTimeoutMs = 30_000;
const inheritedBootstrapEnvironmentKeys = [
    "PATH",
    "Path",
    "PSModulePath",
    "HOME",
    "USERPROFILE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "LOCALAPPDATA",
    "APPDATA",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
];

/** The per-bundle UI state persisted in the workspace's state.json. */
export type BundleState = {
    localizationCode: string | null;
    configFilePaths: StringMap;
    fieldValues: ValueMap;
    checkedOptions: Record<string, string[]>;
    selectedPageID: string | null;
    setupRun: SetupRun | null;
    iconSet: "emoji" | "platform";
    colorTheme: "light" | "dark" | "system";
    webUIFont: "sfPro" | "system";
};

/** A partial state update; appearance values are normalized and setup runs are stored as given. */
export type BundleStateUpdate = {
    [Key in Exclude<keyof BundleState, "iconSet" | "colorTheme" | "webUIFont" | "setupRun">]?: BundleState[Key];
} & {
    setupRun?: SetupRun | LooseRecord | null;
    iconSet?: unknown;
    colorTheme?: unknown;
    webUIFont?: unknown;
};

type BootstrapScript = ConfigBootstrapScriptSpec;

interface ConfigDocument {
    url: string;
    contents: string;
}

const bootstrapDocumentCache = new Map<string, Promise<ConfigDocument>>();
export function normalizeIconSet(value: unknown): BundleState["iconSet"] {
    return value === "emoji" ? "emoji" : "platform";
}
export function normalizeColorTheme(value: unknown): BundleState["colorTheme"] {
    return value === "light" || value === "dark" ? value : "system";
}
export function normalizeWebUIFont(value: unknown): BundleState["webUIFont"] {
    return value === "sfPro" ? "sfPro" : "system";
}
export function bundleStatePath(bundleRoot: string) {
    return path.join(bundleRoot, "state.json");
}
export function emptyBundleState(): BundleState {
    return {
        localizationCode: null,
        configFilePaths: {},
        fieldValues: {},
        checkedOptions: {},
        selectedPageID: null,
        setupRun: null,
        iconSet: "platform",
        colorTheme: "system",
        webUIFont: "system",
    };
}
let saveBundleStateQueue: Promise<unknown> = Promise.resolve();
const bundleStateWarnings = new Map<string, string>();

/**
 * Reads state.json. A file that does not parse is moved aside (never silently overwritten) and a warning
 * is queued for `takeBundleStateWarning`, so the UI can tell the user their saved state was reset.
 */
export async function loadBundleState(bundleRoot: string): Promise<BundleState> {
    const statePath = bundleStatePath(bundleRoot);
    let text: string;
    try {
        text = await readFile(statePath, "utf8");
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT") {
            return emptyBundleState();
        }
        throw error;
    }
    let state: Partial<BundleState>;
    try {
        const parsed: unknown = JSON.parse(text);
        if (parsed == null || typeof parsed !== "object" || Array.isArray(parsed)) {
            throw new Error("expected a JSON object");
        }
        state = parsed;
    }
    catch (error) {
        const backupPath = `${statePath}.corrupt-${new Date().toISOString().replaceAll(":", "-")}`;
        await rename(statePath, backupPath);
        const warning = `Saved app state could not be read (${errorMessage(error)}); it was reset and the old file was kept at ${backupPath}.`;
        console.warn(warning);
        bundleStateWarnings.set(bundleRoot, warning);
        return emptyBundleState();
    }
    return {
        localizationCode: state.localizationCode ?? null,
        configFilePaths: state.configFilePaths ?? {},
        fieldValues: state.fieldValues ?? {},
        checkedOptions: state.checkedOptions ?? {},
        selectedPageID: state.selectedPageID ?? null,
        setupRun: state.setupRun ?? null,
        iconSet: normalizeIconSet(state.iconSet),
        colorTheme: normalizeColorTheme(state.colorTheme),
        webUIFont: normalizeWebUIFont(state.webUIFont),
    };
}
export function takeBundleStateWarning(bundleRoot: string): string | undefined {
    const warning = bundleStateWarnings.get(bundleRoot);
    bundleStateWarnings.delete(bundleRoot);
    return warning;
}
export async function saveBundleState(partialState: BundleStateUpdate, bundleRoot: string): Promise<BundleState> {
    const saveOperation = saveBundleStateQueue.then(async () => {
        const current = await loadBundleState(bundleRoot);
        const next: BundleState = {
            localizationCode: Object.hasOwn(partialState, "localizationCode") ? partialState.localizationCode ?? null : current.localizationCode,
            configFilePaths: partialState.configFilePaths ?? current.configFilePaths,
            fieldValues: partialState.fieldValues ?? current.fieldValues,
            checkedOptions: partialState.checkedOptions ?? current.checkedOptions,
            selectedPageID: Object.hasOwn(partialState, "selectedPageID") ? partialState.selectedPageID ?? null : current.selectedPageID,
            // Setup runs are validated as objects at the request boundary and stored as reported.
            setupRun: Object.hasOwn(partialState, "setupRun") ? (partialState.setupRun ?? null) as SetupRun | null : current.setupRun,
            iconSet: Object.hasOwn(partialState, "iconSet") ? normalizeIconSet(partialState.iconSet) : current.iconSet,
            colorTheme: Object.hasOwn(partialState, "colorTheme")
                ? normalizeColorTheme(partialState.colorTheme)
                : current.colorTheme,
            webUIFont: Object.hasOwn(partialState, "webUIFont") ? normalizeWebUIFont(partialState.webUIFont) : current.webUIFont,
        };
        await writeFileAtomically(bundleStatePath(bundleRoot), `${JSON.stringify(next, null, 2)}\n`);
        return next;
    });
    saveBundleStateQueue = saveOperation.then(() => undefined, () => undefined);
    return saveOperation;
}
/**
 * Writes via a sibling temp file and rename, so readers never observe a truncated file. Symlinks are
 * resolved first and an existing file's permissions are kept.
 */
export async function writeFileAtomically(filePath: string, contents: string) {
    const target = await realpath(filePath).catch((error) => {
        if (errnoCode(error) === "ENOENT") {
            return filePath;
        }
        throw error;
    });
    const existingMode = await stat(target).then((info) => info.mode & 0o7777, () => undefined);
    await mkdir(path.dirname(target), { recursive: true });
    const temporaryPath = `${target}.${process.pid}.${randomUUID()}.tmp`;
    try {
        await writeFile(temporaryPath, contents, { encoding: "utf8", ...(existingMode == null ? {} : { mode: existingMode }) });
        await rename(temporaryPath, target);
    }
    catch (error) {
        await rm(temporaryPath, { force: true });
        throw error;
    }
}
export function initialConfigFilePaths(manifest: BundleManifest, bundleState: Pick<BundleState, "configFilePaths">, bundleRoot: string): StringMap {
    return Object.fromEntries(configEditorControls(manifest).flatMap((control) => {
        if (!control.configFile) {
            return [];
        }
        const persistedPath = bundleState.configFilePaths?.[control.id];
        const defaultPath = control.configFile.path;
        return [[
            control.id,
            persistedPath && isAllowedConfigPath(control, persistedPath, bundleRoot)
                ? persistedPath
                : defaultPath,
        ]];
    }));
}
function isAllowedConfigPath(control: ControlSpec, requestedPath: string, bundleRoot: string) {
    try {
        configPath(control, requestedPath, bundleRoot);
        return true;
    }
    catch {
        return false;
    }
}
const bootstrapQueues = new Map<string, Promise<void>>();
/**
 * Creates or completes the bundle's config files. Calls for one bundle run one at a time: the startup
 * preload and the window's first request both load the bundle, and two first-run bootstraps would each
 * see no config file and rename onto the same target at once, which Windows refuses (EPERM).
 */
export function bootstrapConfigFiles(manifest: BundleManifest, bundleRoot: string, configFilePaths: StringMap): Promise<void> {
    const key = path.resolve(bundleRoot);
    const run = (bootstrapQueues.get(key) ?? Promise.resolve())
        .then(() => bootstrapConfigFilesNow(manifest, bundleRoot, configFilePaths));
    const settled = run.then(() => undefined, () => undefined);
    bootstrapQueues.set(key, settled);
    void settled.then(() => {
        if (bootstrapQueues.get(key) === settled) {
            bootstrapQueues.delete(key);
        }
    });
    return run;
}
async function bootstrapConfigFilesNow(manifest: BundleManifest, bundleRoot: string, configFilePaths: StringMap) {
    for (const control of configEditorControls(manifest)) {
        const configFile = control.configFile;
        const bootstrap = configFile?.bootstrap;
        if (!configFile || !bootstrap || bootstrap.mode === "none") {
            continue;
        }
        const defaultURL = configPath(control, configFilePaths[control.id], bundleRoot);
        const mode = bootstrap.mode ?? "createIfMissing";
        const document = await bootstrapDocument(control, bundleRoot, defaultURL, bootstrap.script);
        const defaults = parseFlatToml(document.contents);
        const existing = await readOptionalFlatToml(document.url);
        if (shouldSkipBootstrap(mode, existing, Object.keys(defaults))) {
            continue;
        }
        await bootstrapToml(mode, document.url, defaults);
    }
}
function shouldSkipBootstrap(mode: string, existing: ValueMap | null, defaultKeys: string[]) {
    if (mode === "createIfMissing") {
        return existing != null;
    }
    if (mode !== "mergeMissing" || existing == null) {
        return false;
    }
    return defaultKeys.every((key) => Object.hasOwn(existing, key));
}
async function bootstrapDocument(control: ControlSpec, bundleRoot: string, defaultURL: string, script: BootstrapScript | undefined): Promise<ConfigDocument> {
    if (!script) {
        return {
            url: defaultURL,
            contents: serializeFlatToml(Object.fromEntries((control.settings ?? []).map((setting) => [settingKey(setting), setting.value ?? ""]))),
        };
    }
    const cacheKey = JSON.stringify({
        controlID: control.id,
        defaultURL,
        script,
    });
    const cachedDocument = bootstrapDocumentCache.get(cacheKey);
    if (cachedDocument) {
        return cachedDocument;
    }
    const document = loadScriptBootstrapDocument(script, control, bundleRoot, defaultURL)
        .catch((error) => {
            bootstrapDocumentCache.delete(cacheKey);
            throw error;
        });
    bootstrapDocumentCache.set(cacheKey, document);
    return document;
}
async function loadScriptBootstrapDocument(script: BootstrapScript, control: ControlSpec, bundleRoot: string, defaultURL: string): Promise<ConfigDocument> {
    const payload = await runBootstrapScript(script, control, bundleRoot, defaultURL);
    const payloadPath = String(payload.path ?? "").trim();
    return {
        url: payloadPath ? resolveConfigFilePath(payloadPath, bundleRoot) : defaultURL,
        contents: await scriptContents(payload, bundleRoot),
    };
}
async function runBootstrapScript(script: BootstrapScript, control: ControlSpec, bundleRoot: string, defaultURL: string): Promise<LooseRecord> {
    resolveBundledPath(script.path, bundleRoot, false);
    const scriptPath = await resolvePlatformScriptPath(script.path, bundleRoot);
    if (!existsSync(scriptPath)) {
        throw new Error(`Config bootstrap script does not exist: ${scriptPath}`);
    }
    const workingDirectory = resolveBundledPath(script.workingDirectory ?? "", bundleRoot, false);
    const command = bootstrapCommand(scriptPath);
    const args = [
        ...command.args,
        ...(script.arguments ?? []).map((argument) => expandConfigPath(argument, bundleRoot, defaultURL)),
    ];
    const stdout = await new Promise<string>((resolve, reject) => {
        execFile(command.executable, args, {
            cwd: workingDirectory,
            env: scriptEnvironment(script, control, bundleRoot, defaultURL),
            maxBuffer: 1024 * 1024,
            timeout: bootstrapScriptTimeoutMs,
            killSignal: "SIGTERM",
        }, (error, stdout, stderr) => {
            if (error) {
                const reason = error.killed ? `timed out after ${bootstrapScriptTimeoutMs}ms` : "failed";
                reject(new Error(`Config bootstrap script ${reason}: ${scriptPath}\n${[stdout, stderr].filter(Boolean).join("\n")}`));
                return;
            }
            resolve(stdout);
        });
    });
    let payload: unknown;
    try {
        payload = JSON.parse(stdout.trim());
    }
    catch (_error) {
        throw new Error(`Config bootstrap script did not return valid JSON: ${scriptPath}`);
    }
    if (payload == null || typeof payload !== "object" || Array.isArray(payload)) {
        throw new Error(`Config bootstrap script did not return a JSON object: ${scriptPath}`);
    }
    return payload as LooseRecord;
}
function bootstrapCommand(scriptPath: string) {
    if (process.platform === "win32" && path.extname(scriptPath).toLowerCase() === ".ps1") {
        return {
            executable: "powershell.exe",
            args: ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", scriptPath],
        };
    }
    return { executable: bootstrapShell(), args: [scriptPath] };
}
function bootstrapShell() {
    if (process.platform !== "win32") {
        return "/bin/sh";
    }
    const gitShell = "C:\\Program Files\\Git\\bin\\sh.exe";
    return existsSync(gitShell) ? gitShell : "sh";
}
function scriptEnvironment(script: BootstrapScript, control: ControlSpec, bundleRoot: string, defaultURL: string): NodeJS.ProcessEnv {
    return {
        ...Object.fromEntries(inheritedBootstrapEnvironmentKeys.flatMap((key) => process.env[key] == null ? [] : [[key, process.env[key]]])),
        GUI_FOR_CLI_BUNDLE_ROOT: bundleRoot,
        GUI_FOR_CLI_BUNDLE_WORKSPACE: bundleRoot,
        GUI_FOR_CLI_CONFIG_PATH: defaultURL,
        GUI_FOR_CLI_CONFIG_DIR: path.dirname(defaultURL),
        GUI_FOR_CLI_CONFIG_CONTROL_ID: control.id,
        GUI_FOR_CLI_CONFIG_CONTROL_LABEL: control.label,
        GUI_FOR_CLI_DRY_RUN: "0",
        ...Object.fromEntries(Object.entries(script.environment ?? {}).map(([key, value]) => [
            key,
            expandConfigPath(value, bundleRoot, defaultURL),
        ])),
    };
}
async function scriptContents(payload: LooseRecord, bundleRoot: string): Promise<string> {
    if (payload.contents != null) {
        return String(payload.contents);
    }
    const contentsPath = String(payload.contentsPath ?? "").trim();
    if (contentsPath) {
        return readFile(resolveConfigFilePath(contentsPath, bundleRoot), "utf8");
    }
    if (payload.values != null && typeof payload.values === "object") {
        return serializeFlatToml(payload.values as LooseRecord);
    }
    return "";
}
async function bootstrapToml(mode: string, url: string, defaults: ValueMap) {
    const existing = await readOptionalFlatToml(url);
    if (mode === "createIfMissing" && existing != null) {
        return;
    }
    const next = mode === "mergeMissing" && existing
        ? { ...existing }
        : { ...defaults };
    if (mode === "mergeMissing" && existing) {
        for (const [key, value] of Object.entries(defaults)) {
            // A key that is there but blank is the user's choice: a blank log directory turns logging off.
            if (!Object.hasOwn(existing, key)) {
                next[key] = value;
            }
        }
    }
    if (mode !== "createIfMissing" && mode !== "mergeMissing") {
        throw new Error(`Unsupported config bootstrap mode: ${mode}`);
    }
    await writeFileAtomically(url, serializeFlatToml(next));
}
async function readOptionalFlatToml(filePath: string): Promise<ValueMap | null> {
    try {
        return parseFlatToml(await readFile(filePath, "utf8"));
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT") {
            return null;
        }
        throw error;
    }
}
export async function initialConfigValues(manifest: BundleManifest, configFilePaths: StringMap, bundleRoot: string, warnings: string[] = []): Promise<ValueMap> {
    const values: ValueMap = Object.fromEntries(configEditorControls(manifest).flatMap((control) => (control.settings ?? []).map((setting) => [`${control.id}.${setting.id}`, setting.value ?? ""])));
    for (const control of configEditorControls(manifest)) {
        const requestedPath = configFilePaths[control.id];
        if (!control.configFile || !requestedPath)
            continue;
        try {
            const fileValues = parseFlatToml(await readFile(configPath(control, requestedPath, bundleRoot), "utf8"));
            for (const setting of control.settings ?? []) {
                const fileValue = fileValues[settingKey(setting)];
                if (fileValue != null) {
                    values[`${control.id}.${setting.id}`] = loadedSettingValue(setting, fileValue, warnings);
                }
            }
        }
        catch (error) {
            if (errnoCode(error) !== "ENOENT")
                throw error;
        }
    }
    return values;
}
function resolveConfigFilePath(value: string, bundleRoot: string) {
    const expanded = expandPathTokens(value, bundleRoot);
    return path.isAbsolute(expanded) ? expanded : path.resolve(bundleRoot, expanded);
}
function expandConfigPath(value: unknown, bundleRoot: string, configURL: string) {
    return expandPathTokens(String(value ?? ""), bundleRoot).replaceAll("{{configPath}}", configURL).replaceAll("{{configDir}}", path.dirname(configURL));
}
function resolveBundledPath(value: string, bundleRoot: string, mustExist: boolean) {
    if (!value) {
        return bundleRoot;
    }
    if (path.isAbsolute(value) || value.split(/[\\/]/).includes("..")) {
        throw new Error(`Config bootstrap script path must be relative and stay inside the bundle: ${value}`);
    }
    const candidate = path.resolve(bundleRoot, value);
    const relative = path.relative(bundleRoot, candidate);
    if (relative.startsWith("..") || path.isAbsolute(relative)) {
        throw new Error(`Config bootstrap script path must be relative and stay inside the bundle: ${value}`);
    }
    if (mustExist && !existsSync(candidate)) {
        throw new Error(`Config bootstrap script does not exist: ${candidate}`);
    }
    return candidate;
}
export function initialFieldValues(manifest: BundleManifest, configValues: ValueMap, bundleState: Pick<BundleState, "fieldValues">): ValueMap {
    const values: ValueMap = Object.fromEntries(allControls(manifest)
        .filter((control) => persistsFieldValue(control.kind))
        .map((control) => [control.id, control.value ?? ""]));
    for (const control of allControls(manifest).filter((control) => persistsFieldValue(control.kind))) {
        const persisted = bundleState.fieldValues?.[control.id];
        if (!configSettingBindings(manifest, control.id).length && persisted != null) {
            values[control.id] = persisted;
        }
    }
    for (const control of configEditorControls(manifest)) {
        for (const setting of control.settings ?? []) {
            const value = configValues[`${control.id}.${setting.id}`] ?? setting.value ?? "";
            if (Object.hasOwn(values, settingKey(setting)))
                values[settingKey(setting)] = value;
            if (Object.hasOwn(values, setting.id))
                values[setting.id] = value;
        }
    }
    return values;
}
export function initialCheckedOptions(manifest: BundleManifest, configValues: ValueMap, bundleState: Pick<BundleState, "checkedOptions">): Record<string, string[]> {
    const values: Record<string, string[]> = {};
    for (const control of allControls(manifest).filter((candidate) => candidate.kind === "checkboxGroup")) {
        const binding = configSettingBindings(manifest, control.id)[0];
        if (binding) {
            const configValue = configValues[`${binding.control.id}.${binding.setting.id}`] ?? binding.setting.value ?? "";
            values[control.id] = String(configValue)
                .split(",")
                .map((item) => item.trim())
                .filter(Boolean);
        }
        else if (bundleState.checkedOptions?.[control.id]) {
            values[control.id] = bundleState.checkedOptions[control.id] ?? [];
        }
        else {
            values[control.id] = (control.options ?? []).filter((option) => option.selected).map((option) => option.id);
        }
    }
    return values;
}
export function configSettingBindings(manifest: BundleManifest, fieldID: string): Array<{ control: ControlSpec; setting: ConfigSetting }> {
    return configEditorControls(manifest).flatMap((control) => (control.settings ?? [])
        .filter((setting) => setting.id === fieldID || setting.key === fieldID)
        .map((setting) => ({ control, setting })));
}
export async function loadConfig(control: ControlSpec, requestedPath: string | undefined, bundleRoot: string) {
    const filePath = configPath(control, requestedPath, bundleRoot);
    const warnings: string[] = [];
    let values: ValueMap = {};
    try {
        values = parseFlatToml(await readFile(filePath, "utf8"));
    }
    catch (error) {
        if (errnoCode(error) !== "ENOENT") {
            throw error;
        }
    }
    const loadedValues = Object.fromEntries((control.settings ?? []).map((setting) => [
            settingKey(setting),
            loadedSettingValue(setting, values[settingKey(setting)] ?? setting.value ?? "", warnings),
        ]));
    return {
        path: filePath,
        values: loadedValues,
        warnings,
    };
}
export async function saveConfig(control: ControlSpec, requestedPath: string | undefined, values: Readonly<Record<string, unknown>>, bundleRoot: string) {
    const filePath = configPath(control, requestedPath, bundleRoot);
    const byKey: Record<string, unknown> = {};
    for (const setting of control.settings ?? []) {
        const key = settingKey(setting);
        const value = typedSettingValue(
            setting,
            values[key] ?? values[setting.id] ?? values[`${control.id}.${setting.id}`] ?? setting.value ?? "",
        );
        // The CLI reads this file too, and knows only real paths.
        byKey[key] = setting.kind === "path" && typeof value === "string" ? expandWorkspaceToken(value, bundleRoot) : value;
    }
    await writeFileAtomically(filePath, serializeFlatToml(byKey));
    return { path: filePath, keyCount: Object.keys(byKey).length };
}

/** The settings-file key a config setting is stored under (required by the schema). */
function settingKey(setting: ConfigSetting): string {
    return setting.key ?? setting.id;
}

function loadedSettingValue(setting: ConfigSetting, value: unknown, warnings: string[]): StateValue {
    try {
        const expectedType = setting.kind === "toggle" || typeof setting.value === "boolean"
            ? "boolean" : typeof setting.value === "number" ? "number" : "string";
        if (typeof value !== expectedType) {
            throw new Error(`Invalid ${expectedType} value for setting ${settingKey(setting)}.`);
        }
        return typedSettingValue(setting, value);
    }
    catch (error) {
        const warning = `${errorMessage(error)} Ignoring it and using the declared default.`;
        warnings.push(warning);
        console.warn(warning);
        return typedSettingValue(setting, setting.value ?? "");
    }
}

function typedSettingValue(setting: ConfigSetting, value: unknown): StateValue {
    if (setting.kind === "toggle" || typeof setting.value === "boolean") {
        if (typeof value === "boolean") {
            return value;
        }
        if (value === "") {
            return setting.value === true;
        }
        if (value === "true" || value === "false") {
            return value === "true";
        }
        throw new Error(`Invalid boolean value for setting ${settingKey(setting)}.`);
    }
    if (typeof setting.value === "number") {
        const number = typeof value === "string" && value.trim() === "" ? setting.value : Number(value);
        if ((typeof value !== "string" && typeof value !== "number") || !Number.isFinite(number)) {
            throw new Error(`Invalid number value for setting ${settingKey(setting)}.`);
        }
        return number;
    }
    if (typeof value !== "string") {
        throw new Error(`Invalid string value for setting ${settingKey(setting)}.`);
    }
    return value;
}
