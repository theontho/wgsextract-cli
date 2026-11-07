import { stat, statfs } from "node:fs/promises";
import { homedir, platform } from "node:os";
import path from "node:path";
import { commandLine, evaluateNumeric, interpolate, renderedCommand } from "../../../shared/rendering.js";
import type { ActionSpec, BundleManifest, CommandContext, DataSourcePayload, Labels, LooseRecord, ProcessResult, RunProcess, StringMap, ValueMap } from "../../../shared/types.js";
import { decodeXML, environmentKey, formatGB, resolveBundlePath, resolveUserPath } from "./paths.js";
import { platformCommand } from "./platform-command.js";
import { isPlatformScriptReference, resolvePlatformScriptPath } from "./platform-scripts.js";
import { errnoCode, errorMessage } from "./errors.js";

const dataSourceTimeoutMs = 15_000;

export type ActionRunResult = ProcessResult & {
    startedAt: string;
    finishedAt: string;
    command: string;
};

export type ActionEvent =
    | { type: "start"; command: string; startedAt: string }
    | { type: "output"; stream: "stdout" | "stderr"; text: string }
    | { type: "complete"; result: ActionRunResult };

export type ActionEmit = (event: ActionEvent) => unknown;

interface DataSourceSpec {
    path: string;
    arguments: string[];
    environment: StringMap;
    workingDirectory?: string;
}

export type PrecheckEvaluation = {
    severity: "warning" | "info";
    title: string;
    message: string;
    requiredGB: number;
    availableGB: number;
    path: string;
    pathLabel: string;
};

export async function runAction(
    action: ActionSpec,
    context: CommandContext,
    signal: AbortSignal | undefined,
    bundleRoot: string,
    runProcess: RunProcess,
    emit: ActionEmit = () => { },
): Promise<ActionRunResult> {
    if (!action?.command) {
        throw new Error("Missing action command.");
    }
    const resolvedContext = await contextWithFileState(context, bundleRoot);
    const rendered = renderedCommand(action.command, resolvedContext);
    const executable = isPlatformScriptReference(rendered.executable, bundleRoot)
        ? await resolvePlatformScriptPath(rendered.executable, bundleRoot)
        : rendered.executable;
    const display = await platformCommand(executable, rendered.arguments ?? []);
    const startedAt = new Date().toISOString();
    await emit({ type: "start", command: commandLine(display.executable, display.args), startedAt });
    const result = await runProcess(executable, rendered.arguments ?? [], {
        cwd: bundleRoot,
        env: { ...process.env, GUI_FOR_CLI_BUNDLE_ROOT: bundleRoot, GUI_FOR_CLI_BUNDLE_WORKSPACE: bundleRoot },
        ...(signal ? { signal } : {}),
        onStdout: (text) => void emit({ type: "output", stream: "stdout", text }),
        onStderr: (text) => void emit({ type: "output", stream: "stderr", text }),
    });
    const actionResult: ActionRunResult = {
        ...result,
        startedAt,
        finishedAt: new Date().toISOString(),
        command: commandLine(display.executable, display.args),
    };
    await emit({ type: "complete", result: actionResult });
    return actionResult;
}

/** Validates a manifest data source declaration (bundle JSON, so untyped until here). */
function dataSourceSpec(value: LooseRecord | undefined): DataSourceSpec {
    const sourcePath = value?.path;
    if (typeof sourcePath !== "string" || !sourcePath) {
        throw new Error("Missing data source path.");
    }
    const args = value?.arguments ?? [];
    const environment = value?.environment ?? {};
    const workingDirectory = value?.workingDirectory;
    if (!Array.isArray(args) || !args.every((argument) => typeof argument === "string")) {
        throw new Error(`Data source ${sourcePath} arguments must be strings.`);
    }
    if (environment == null || typeof environment !== "object" || !Object.values(environment).every((item) => typeof item === "string")) {
        throw new Error(`Data source ${sourcePath} environment values must be strings.`);
    }
    if (workingDirectory != null && typeof workingDirectory !== "string") {
        throw new Error(`Data source ${sourcePath} workingDirectory must be a string.`);
    }
    return { path: sourcePath, arguments: args, environment: environment as StringMap, ...(workingDirectory ? { workingDirectory } : {}) };
}

export async function runDataSource(dataSourceValue: LooseRecord | undefined, context: CommandContext, bundleRoot: string, runProcess: RunProcess): Promise<DataSourcePayload> {
    const { path: sourcePath, stdout } = await runDataSourceScript(dataSourceValue, context, bundleRoot, runProcess);
    try {
        return JSON.parse(stdout || "{}") as DataSourcePayload;
    }
    catch (error) {
        throw new Error(`Data source ${sourcePath} did not print valid JSON: ${errorMessage(error)}`);
    }
}

/** Runs a data source's script and returns what it printed, unparsed. */
export async function runDataSourceScript(
    dataSourceValue: LooseRecord | undefined,
    context: CommandContext,
    bundleRoot: string,
    runProcess: RunProcess,
    timeoutMs = dataSourceTimeoutMs,
): Promise<{ path: string; stdout: string }> {
    const dataSource = dataSourceSpec(dataSourceValue);
    const executable = await resolvePlatformScriptPath(dataSource.path, bundleRoot);
    const workingDirectory = dataSource.workingDirectory ? resolveBundlePath(dataSource.workingDirectory, bundleRoot) : bundleRoot;
    const resolvedContext = await contextWithFileState(context, bundleRoot);
    const args = dataSource.arguments.map((argument) => interpolate(argument, resolvedContext));
    const env: Record<string, string | undefined> = {
        ...process.env,
        GUI_FOR_CLI_BUNDLE_ROOT: bundleRoot,
        GUI_FOR_CLI_BUNDLE_WORKSPACE: bundleRoot,
        GUI_FOR_CLI_DATA_SOURCE: "1",
    };
    for (const [key, value] of Object.entries(context.fieldValues ?? {})) {
        env[`GUI_FOR_CLI_FIELD_${environmentKey(key)}`] = value === undefined ? undefined : String(value);
    }
    for (const [key, value] of Object.entries(context.configValues ?? {})) {
        env[`GUI_FOR_CLI_CONFIG_${environmentKey(key)}`] = value === undefined ? undefined : String(value);
    }
    for (const [key, value] of Object.entries(dataSource.environment)) {
        env[key] = interpolate(value, resolvedContext);
    }
    const result = await runProcess(executable, args, { cwd: workingDirectory, env, timeoutMs });
    if (result.exitCode !== 0) {
        throw new Error(`Data source ${dataSource.path} exited ${result.exitCode}: ${result.stderr || "no stderr"}`);
    }
    return { path: dataSource.path, stdout: result.stdout };
}
export async function contextWithFileState(context: CommandContext, bundleRoot: string): Promise<CommandContext> {
    return { ...context, fileStateValues: await fileStateValues(context, bundleRoot) };
}
export async function fileStateValues(context: CommandContext, bundleRoot: string): Promise<StringMap> {
    const values: StringMap = {};
    const sourceValues: ValueMap = {
        ...(context.fieldValues ?? {}),
        ...(context.configValues ?? {}),
        ...(context.rowValues ?? {}),
    };
    for (const [id, rawPath] of Object.entries(sourceValues)) {
        Object.assign(values, await fileStateForPath(id, rawPath, bundleRoot));
    }
    return values;
}
async function fileStateForPath(id: string, rawPath: unknown, bundleRoot: string): Promise<StringMap> {
    const pathText = String(rawPath ?? "").trim();
    const values: StringMap = {
        [`${id}.pathExtension`]: "",
        [`${id}.isIndexed`]: "false",
        [`${id}.isSorted`]: "false",
        [`${id}.exists`]: "false",
        [`${id}.fileSize`]: "",
        [`${id}.fileSizeGB`]: "",
        [`${id}.parentDir`]: "",
    };
    if (!pathText) {
        return values;
    }
    const resolvedPath = resolveUserPath(pathText, bundleRoot);
    values[`${id}.pathExtension`] = path.extname(resolvedPath).replace(/^\./, "").toLowerCase();
    values[`${id}.parentDir`] = path.dirname(resolvedPath);
    values[`${id}.isIndexed`] = String(await isIndexedAlignment(resolvedPath));
    values[`${id}.isSorted`] = String(await isSortedAlignment(resolvedPath));
    try {
        const info = await stat(resolvedPath);
        values[`${id}.exists`] = "true";
        if (info.isFile()) {
            values[`${id}.fileSize`] = String(info.size);
            values[`${id}.fileSizeGB`] = (Number(info.size) / 1_073_741_824).toFixed(2);
        }
    }
    catch (error) {
        if (errnoCode(error) !== "ENOENT" && errnoCode(error) !== "ENOTDIR") {
            throw error;
        }
    }
    return values;
}
async function isIndexedAlignment(resolvedPath: string): Promise<boolean> {
    const parsed = path.parse(resolvedPath);
    const withoutExtension = path.join(parsed.dir, parsed.name);
    const indexPaths = [
        `${resolvedPath}.bai`,
        `${resolvedPath}.crai`,
        `${resolvedPath}.csi`,
        `${withoutExtension}.bai`,
        `${withoutExtension}.crai`,
        `${withoutExtension}.csi`,
    ];
    return (await Promise.all(indexPaths.map((candidate) => pathExists(candidate)))).some(Boolean);
}
async function isSortedAlignment(resolvedPath: string): Promise<boolean> {
    if (await isIndexedAlignment(resolvedPath)) {
        return true;
    }
    const filename = path.basename(resolvedPath).toLowerCase();
    return (filename.includes(".sorted.") ||
        filename.includes("_sorted.") ||
        filename.endsWith(".sorted.bam") ||
        filename.endsWith(".sorted.cram") ||
        filename.includes(".sort.") ||
        filename.includes("_sort."));
}
async function pathExists(filePath: string): Promise<boolean> {
    try {
        await stat(filePath);
        return true;
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT" || errnoCode(error) === "ENOTDIR") {
            return false;
        }
        throw error;
    }
}
export async function evaluatePrecheck(
    precheck: LooseRecord | undefined,
    context: CommandContext,
    labels: Labels,
    bundleRoot: string,
    runProcess: RunProcess,
): Promise<PrecheckEvaluation | null> {
    if (!precheck?.diskSpaceGB) {
        return null;
    }
    const interpolated = await interpolatePrecheck(precheck.diskSpaceGB, context, bundleRoot);
    const requiredGB = evaluateNumeric(interpolated);
    if (!Number.isFinite(requiredGB) || requiredGB <= 0) {
        return null;
    }
    const pathExpression = precheck.diskSpacePath || "{{out_dir}}";
    let targetPath = (await interpolatePrecheck(pathExpression, context, bundleRoot)).trim();
    if (!targetPath) {
        targetPath = context.bundleRootPath || homedir();
    }
    const expandedPath = resolveUserPath(targetPath, bundleRoot);
    const availableGB = await volumeAvailableGB(expandedPath, bundleRoot);
    if (!Number.isFinite(availableGB)) {
        return null;
    }
    const severity: PrecheckEvaluation["severity"] = availableGB < requiredGB ? "warning" : "info";
    const required = formatGB(requiredGB);
    const available = formatGB(availableGB);
    const pathLabel = await diskPathLabel(expandedPath, bundleRoot, runProcess);
    const title = severity === "warning"
        ? labels.actionPrecheckDiskSpaceTitle || "Not enough free disk space"
        : labels.actionPrecheckDiskSpaceInfoTitle || "Disk space estimate";
    const format = severity === "warning" && precheck.warningMessage
        ? await interpolatePrecheck(precheck.warningMessage, context, bundleRoot)
        : severity === "warning"
            ? labels.actionPrecheckDiskSpaceMessageFormat ||
                "Need %{required} GB free at %{path}, only %{available} GB available."
            : labels.actionPrecheckDiskSpaceInfoFormat ||
                "Estimated %{required} GB needed at %{path} (%{available} GB free).";
    return {
        severity,
        title,
        message: String(format)
            .replaceAll("%{required}", required)
            .replaceAll("%{available}", available)
            .replaceAll("%{path}", pathLabel),
        requiredGB,
        availableGB,
        path: expandedPath,
        pathLabel,
    };
}
export function setupInstallSizePrecheck(manifest: Pick<BundleManifest, "setup">): LooseRecord | null {
    const requiredGB = Number(manifest?.setup?.initialInstallSizeGB);
    if (!Number.isFinite(requiredGB) || requiredGB <= 0) {
        return null;
    }
    return {
        diskSpaceGB: String(requiredGB),
        diskSpacePath: "{{bundleRoot}}",
    };
}
export async function evaluateSetupInstallSizePrecheck(manifest: Pick<BundleManifest, "setup">, labels: Labels, bundleRoot: string, runProcess: RunProcess): Promise<PrecheckEvaluation | null> {
    const precheck = setupInstallSizePrecheck(manifest);
    if (!precheck) {
        return null;
    }
    return evaluatePrecheck(
        precheck,
        { fieldValues: {}, checkedOptions: {}, configValues: {}, rowValues: {}, bundleRootPath: bundleRoot },
        labels,
        bundleRoot,
        runProcess,
    );
}
async function interpolatePrecheck(value: unknown, context: CommandContext, bundleRoot: string): Promise<string> {
    let output = "";
    let cursor = 0;
    for (const match of String(value ?? "").matchAll(/\{\{([^}]+)\}\}/g)) {
        const placeholder = match[1];
        if (placeholder == null || match.index == null) {
            continue;
        }
        output += String(value).slice(cursor, match.index);
        output += (await precheckContextValue(context, placeholder.trim(), bundleRoot)) ?? "";
        cursor = match.index + match[0].length;
    }
    output += String(value ?? "").slice(cursor);
    return output;
}
async function precheckContextValue(context: CommandContext, placeholder: string, bundleRoot: string): Promise<string> {
    const separator = placeholder.lastIndexOf(".");
    if (separator > 0 && separator < placeholder.length - 1) {
        const fieldID = placeholder.slice(0, separator);
        const property = placeholder.slice(separator + 1);
        const rawPath = context.fieldValues?.[fieldID] ?? context.configValues?.[fieldID];
        if (property === "fileSizeGB" || property === "fileSize") {
            const bytes = await fileSizeBytes(rawPath, bundleRoot);
            if (!Number.isFinite(bytes)) {
                return "";
            }
            return property === "fileSizeGB" ? String(bytes / 1_073_741_824) : String(bytes);
        }
        if (property === "parentDir") {
            return rawPath ? path.dirname(resolveUserPath(rawPath, bundleRoot)) : "";
        }
    }
    return interpolate(`{{${placeholder}}}`, context);
}
async function fileSizeBytes(rawPath: unknown, bundleRoot: string): Promise<number> {
    if (!rawPath) {
        return Number.NaN;
    }
    try {
        const info = await stat(resolveUserPath(rawPath, bundleRoot));
        return info.isFile() ? info.size : Number.NaN;
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT") {
            return Number.NaN;
        }
        throw error;
    }
}
async function volumeAvailableGB(rawPath: string, bundleRoot: string): Promise<number> {
    let probe = resolveUserPath(rawPath, bundleRoot);
    while (probe && probe !== path.dirname(probe)) {
        try {
            const info = await statfs(probe);
            return Number(info.bavail * info.bsize) / 1_073_741_824;
        }
        catch (error) {
            if (errnoCode(error) !== "ENOENT" && errnoCode(error) !== "ENOTDIR") {
                throw error;
            }
            probe = path.dirname(probe);
        }
    }
    return Number.NaN;
}
async function diskPathLabel(rawPath: string, bundleRoot: string, runProcess: RunProcess): Promise<string> {
    const expanded = resolveUserPath(rawPath, bundleRoot);
    const folderName = path.basename(expanded) || expanded;
    const volumeName = await volumeNameForPath(expanded, bundleRoot, runProcess);
    return volumeName && volumeName !== folderName ? `${folderName} (${volumeName})` : folderName;
}
async function volumeNameForPath(rawPath: string, bundleRoot: string, runProcess: RunProcess): Promise<string | undefined> {
    const probe = await existingAncestor(rawPath, bundleRoot);
    if (platform() === "win32") {
        const root = path.parse(probe).root;
        return root ? root.replace(/[\\/]$/, "") : undefined;
    }
    if (platform() === "darwin") {
        try {
            const result = await runProcess("/usr/sbin/diskutil", ["info", "-plist", probe], {
                cwd: bundleRoot,
                env: process.env,
                maxOutputBytes: 262_144,
                maxErrorBytes: 16_384,
            });
            if (result.exitCode === 0) {
                const match = /<key>VolumeName<\/key>\s*<string>([^<]+)<\/string>/.exec(result.stdout);
                if (match?.[1]) {
                    return decodeXML(match[1]);
                }
            }
        }
        catch (error) {
            if (errorMessage(error) !== "Process cancelled.") {
                console.warn(`Could not read volume name for ${probe}: ${errorMessage(error)}`);
            }
            return undefined;
        }
    }
    return undefined;
}
async function existingAncestor(rawPath: string, bundleRoot: string): Promise<string> {
    let probe = resolveUserPath(rawPath, bundleRoot);
    while (probe && probe !== path.dirname(probe)) {
        try {
            await stat(probe);
            return probe;
        }
        catch (error) {
            if (errnoCode(error) !== "ENOENT" && errnoCode(error) !== "ENOTDIR") {
                throw error;
            }
            probe = path.dirname(probe);
        }
    }
    return probe || resolveUserPath(rawPath, bundleRoot);
}
