import path from "node:path";
import { commandLine, setupResultLine, shellQuote } from "../../../shared/rendering.js";
import { setupStepsForPlatform } from "../../../shared/setup-platforms.js";
import type { BundleManifest, Labels, ProcessResult, RunProcess, SetupRun, SetupStep, StringMap } from "../../../shared/types.js";
import { evaluateSetupInstallSizePrecheck, type PrecheckEvaluation } from "./action-runner.js";
import { expandPathTokens, resolveBundlePath } from "./paths.js";
import { platformCommand } from "./platform-command.js";
import { resolvePlatformScriptPath } from "./platform-scripts.js";
import { errorMessage } from "./errors.js";

const activeWorkspaces = new Set<string>();

type SetupManifest = Pick<BundleManifest, "setup" | "uninstall">;

export interface SetupCommand {
    id: string;
    label: string | undefined;
    kind: string;
    executable: string;
    arguments: string[];
    environment: StringMap;
    workingDirectory: string;
    optional: boolean;
    requiresAdmin: boolean;
    /** Shell-quoted command line shown to the user. */
    command: string;
}

export type SetupCommandResult = ProcessResult & {
    id: string;
    label: string | undefined;
    kind: string;
    command: string;
    status: SetupStatus;
    durationMs: number;
};

export type SetupStatus = "ok" | "warning" | "failed";

export type SetupSummary = {
    status: SetupStatus;
    results: SetupCommandResult[];
    error?: string;
    preflight?: PrecheckEvaluation;
};

export type SetupEvent =
    | { type: "step-start"; step: SetupCommand }
    | { type: "output"; id: string; stream: "stdout" | "stderr"; text: string }
    | { type: "step-complete"; result: SetupCommandResult }
    | { type: "complete"; result: SetupSummary };

export type SetupEmit = (event: SetupEvent) => unknown;

interface ExecutionCommand {
    executable: string;
    arguments: string[];
    cwd: string | undefined;
    env: NodeJS.ProcessEnv;
}

const ignoreSetupEvent: SetupEmit = () => { };

export async function setupCommandForStep(step: SetupStep, bundleRoot: string): Promise<SetupCommand> {
    const workingDirectory = step.workingDirectory ? resolveBundlePath(step.workingDirectory, bundleRoot) : bundleRoot;
    const environment = Object.fromEntries(Object.entries(step.environment ?? {}).map(([key, value]) => [key, expandSetupValue(value, bundleRoot)]));
    const value = expandSetupValue(step.value, bundleRoot);
    const args = (step.arguments ?? []).map((argument) => expandSetupValue(argument, bundleRoot));
    switch (step.kind) {
        case "pathTool":
            return setupCommand(step, "/usr/bin/env", ["which", value], workingDirectory, environment);
        case "homebrewPackage":
            return setupCommand(step, "/usr/bin/env", ["brew", "list", value], workingDirectory, environment);
        case "bundledScript":
        case "setupScript":
            return setupCommand(step, await resolvePlatformScriptPath(step.value ?? "", bundleRoot), args, workingDirectory, environment);
        case "pixiInstall":
            return setupCommand(step, "/usr/bin/env", ["pixi", "install", ...args], workingDirectory, environment);
        case "pixiRun":
            return setupCommand(step, "/usr/bin/env", ["pixi", "run", value, ...args], workingDirectory, environment);
        default:
            throw new Error(`Unsupported setup step kind: ${step.kind}`);
    }
}

export async function runSetupStep(manifest: SetupManifest, bundleRoot: string, runProcess: RunProcess, stepID: string): Promise<SetupCommandResult> {
    const step = setupStepsForPlatform(manifest.setup?.steps ?? []).find((candidate) => candidate.id === stepID);
    if (!step) {
        throw new Error(`Unknown setup step: ${stepID}`);
    }
    return executeSetupStep(step, bundleRoot, runProcess);
}

export async function runSetup(
    manifest: SetupManifest,
    bundleRoot: string,
    runProcess: RunProcess,
    emit: SetupEmit = ignoreSetupEvent,
    labels: Labels = {},
    signal?: AbortSignal,
): Promise<SetupSummary> {
    return withWorkspaceLock(bundleRoot, async () => {
        const preflight = await evaluateSetupInstallSizePrecheck(manifest, labels, bundleRoot, runProcess);
        if (preflight?.severity === "warning") {
            const summary: SetupSummary = { status: "failed", results: [], error: preflight.message, preflight };
            emit({ type: "complete", result: summary });
            return summary;
        }
        return runStepSet(setupStepsForPlatform(manifest.setup?.steps ?? []), bundleRoot, runProcess, emit, signal);
    });
}
export async function runUninstall(
    manifest: SetupManifest,
    bundleRoot: string,
    runProcess: RunProcess,
    emit: SetupEmit = ignoreSetupEvent,
    _labels: Labels = {},
    signal?: AbortSignal,
): Promise<SetupSummary> {
    return withWorkspaceLock(bundleRoot, () => runStepSet(setupStepsForPlatform(manifest.uninstall?.steps ?? []), bundleRoot, runProcess, emit, signal));
}

/** Setup and uninstall mutate the bundle workspace (for example `pixi install`), so only one may run per workspace. */
async function withWorkspaceLock<T>(bundleRoot: string, run: () => Promise<T>): Promise<T> {
    const key = path.resolve(bundleRoot);
    if (activeWorkspaces.has(key)) {
        throw new Error("Setup is already running for this bundle workspace.");
    }
    activeWorkspaces.add(key);
    try {
        return await run();
    }
    finally {
        activeWorkspaces.delete(key);
    }
}

async function runStepSet(steps: SetupStep[], bundleRoot: string, runProcess: RunProcess, emit: SetupEmit, signal?: AbortSignal): Promise<SetupSummary> {
    const results: SetupCommandResult[] = [];
    for (const step of steps) {
        if (signal?.aborted) {
            throw new Error("Setup cancelled.");
        }
        const result = await executeSetupStep(step, bundleRoot, runProcess, emit, signal);
        results.push(result);
        if (result.status === "failed" && !step.optional) {
            break;
        }
    }
    const status = setupRunStatus(results);
    const summary: SetupSummary = { status, results };
    emit({ type: "complete", result: summary });
    return summary;
}

export async function runInitialSetupIfNeeded(
    bundle: { manifest: SetupManifest; bundleState?: { setupRun?: SetupRun | null } | null },
    bundleRoot: string,
    runProcess: RunProcess,
    saveState: (state: { setupRun: SetupRun }) => Promise<unknown>,
    emit: SetupEmit = ignoreSetupEvent,
    enabled = true,
    now = () => new Date().toISOString(),
): Promise<SetupRun | null> {
    if (!enabled || bundle.bundleState?.setupRun || !setupStepsForPlatform(bundle.manifest.setup?.steps ?? []).length) {
        return null;
    }
    const results: SetupCommandResult[] = [];
    const captureAndEmit: SetupEmit = (event) => {
        if (event.type === "step-complete") {
            const index = results.findIndex((result) => result.id === event.result.id);
            if (index >= 0) {
                results[index] = event.result;
            }
            else {
                results.push(event.result);
            }
        }
        emit(event);
    };
    let setupRun: SetupRun;
    try {
        setupRun = { ...(await runSetup(bundle.manifest, bundleRoot, runProcess, captureAndEmit)), completedAt: now() };
    }
    catch (error) {
        setupRun = {
            status: "failed",
            results,
            completedAt: now(),
            error: error instanceof Error ? errorMessage(error) : String(error),
        };
    }
    await saveState({ setupRun });
    bundle.bundleState = { ...(bundle.bundleState ?? {}), setupRun };
    return setupRun;
}

async function executeSetupStep(step: SetupStep, bundleRoot: string, runProcess: RunProcess, emit: SetupEmit = ignoreSetupEvent, signal?: AbortSignal): Promise<SetupCommandResult> {
    const command = await setupCommandForStep(step, bundleRoot);
    const displayedStep = await displaySetupCommand(command);
    emit({ type: "step-start", step: displayedStep });
    const startedAt = Date.now();
    const result = await runSetupCommand(displayedStep, bundleRoot, runProcess, emit, signal);
    const setupResult = setupResultForCommand(displayedStep, result, Math.max(0, Date.now() - startedAt));
    emit({ type: "step-complete", result: setupResult });
    return setupResult;
}

async function displaySetupCommand(command: SetupCommand): Promise<SetupCommand> {
    const displayCommand = await platformCommand(command.executable, command.arguments);
    const renderedCommand = commandLine(displayCommand.executable, displayCommand.args);
    return {
        ...command,
        executable: displayCommand.executable,
        arguments: displayCommand.args,
        command: command.requiresAdmin && process.platform !== "win32" ? `sudo ${renderedCommand}` : renderedCommand,
    };
}

// Setup steps (multi-GB installs) have no wall-clock limit; they end on completion or cancellation.
async function runSetupCommand(command: SetupCommand, bundleRoot: string, runProcess: RunProcess, emit: SetupEmit, signal?: AbortSignal): Promise<ProcessResult> {
    const env: NodeJS.ProcessEnv = {
        ...process.env,
        ...command.environment,
        GUI_FOR_CLI_BUNDLE_ROOT: bundleRoot,
        GUI_FOR_CLI_BUNDLE_WORKSPACE: bundleRoot,
    };
    if (command.requiresAdmin && process.platform === "win32") {
        return runProcess(command.executable, command.arguments, {
            cwd: command.workingDirectory,
            env,
            elevatedEnv: env,
            requiresAdmin: true,
            ...(signal ? { signal } : {}),
            onStdout: (text) => void emit({ type: "output", id: command.id, stream: "stdout", text }),
            onStderr: (text) => void emit({ type: "output", id: command.id, stream: "stderr", text }),
        });
    }
    const executionCommand = adminExecutionCommand(command, bundleRoot, env);
    return runProcess(executionCommand.executable, executionCommand.arguments, {
        ...(executionCommand.cwd ? { cwd: executionCommand.cwd } : {}),
        env: executionCommand.env,
        ...(signal ? { signal } : {}),
        onStdout: (text) => void emit({ type: "output", id: command.id, stream: "stdout", text }),
        onStderr: (text) => void emit({ type: "output", id: command.id, stream: "stderr", text }),
    });
}

function adminExecutionCommand(command: SetupCommand, bundleRoot: string, env: NodeJS.ProcessEnv): ExecutionCommand {
    if (!command.requiresAdmin) {
        return {
            executable: command.executable,
            arguments: command.arguments,
            cwd: command.workingDirectory,
            env,
        };
    }
    if (process.platform === "darwin") {
        const elevatedEnv = elevatedCommandEnvironment(command, bundleRoot);
        const shellScript = [
            `cd ${shellQuote(command.workingDirectory)}`,
            commandLine("/usr/bin/env", [
                ...environmentAssignmentArguments(elevatedEnv),
                command.executable,
                ...command.arguments,
            ]),
        ].join(" && ");
        return {
            executable: "/usr/bin/osascript",
            arguments: ["-e", `do shell script ${appleScriptStringLiteral(shellScript)} with administrator privileges`],
            cwd: undefined,
            env,
        };
    }
    if (process.platform === "win32") {
        throw new Error("Admin setup steps are only supported on macOS and POSIX systems.");
    }
    const elevatedEnv = elevatedCommandEnvironment(command, bundleRoot);
    return {
        executable: "/usr/bin/sudo",
        arguments: [
            "/usr/bin/env",
            ...environmentAssignmentArguments(elevatedEnv),
            command.executable,
            ...command.arguments,
        ],
        cwd: command.workingDirectory,
        env,
    };
}

function elevatedCommandEnvironment(command: SetupCommand, bundleRoot: string): StringMap {
    return {
        PATH: process.env.PATH ?? "/usr/bin:/bin:/usr/sbin:/sbin",
        ...command.environment,
        GUI_FOR_CLI_BUNDLE_ROOT: bundleRoot,
        GUI_FOR_CLI_BUNDLE_WORKSPACE: bundleRoot,
    };
}

function environmentAssignmentArguments(env: StringMap): string[] {
    return Object.entries(env)
        .filter(([key, value]) => value != null && /^[A-Za-z_][A-Za-z0-9_]*$/.test(key))
        .map(([key, value]) => `${key}=${String(value)}`);
}

function appleScriptStringLiteral(value: string): string {
    return `"${value.replaceAll("\\", "\\\\").replaceAll("\"", "\\\"")}"`;
}

function setupResultForCommand(command: SetupCommand, result: ProcessResult, durationMs: number): SetupCommandResult {
    const status: SetupStatus = result.exitCode === 0 ? "ok" : command.optional ? "warning" : "failed";
    return {
        ...result,
        id: command.id,
        label: command.label,
        kind: command.kind,
        command: command.command,
        status,
        durationMs,
    };
}

function setupCommand(step: SetupStep, executable: string, args: string[], workingDirectory: string, environment: StringMap): SetupCommand {
    return {
        id: step.id,
        label: step.label,
        kind: step.kind,
        executable,
        arguments: args,
        environment,
        workingDirectory,
        optional: Boolean(step.optional),
        requiresAdmin: step.requiresAdmin === true,
        command: commandLine(executable, args),
    };
}

function expandSetupValue(value: unknown, bundleRoot: string): string {
    return expandPathTokens(value, bundleRoot);
}

export function setupEventLine(event: SetupEvent): string {
    if (event.type === "step-complete") {
        return setupResultLine(event.result);
    }
    return "";
}

function setupRunStatus(results: SetupCommandResult[]): SetupStatus {
    if (results.some((result) => result.status === "failed")) {
        return "failed";
    }
    return results.some((result) => result.status === "warning") ? "warning" : "ok";
}
