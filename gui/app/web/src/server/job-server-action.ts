import { interpolate } from "../../../shared/rendering.js";
import type { ActionSpec, CommandContext, LooseRecord, RunProcess, ServerOperationSpec } from "../../../shared/types.js";
import { contextWithFileState, runAction, type ActionEmit, type ActionRunResult } from "./action-runner.js";
import { errorMessage } from "./errors.js";
import { cancelJob, followJob, getJob, jobLog, startJob, type JobEvent, type JobServerAddress } from "./job-server-client.js";
import type { JobServer } from "./job-server.js";
import { TailBuffer } from "./tail-buffer.js";

/** How long a cancelled job may take to finish before the run stops waiting for it. */
const cancelWaitMs = 60_000;

/** The result keeps the end of a job's output, as it does for a command; all of it is still streamed. */
const maxOutputBytes = 1_048_576;

/**
 * Runs an action with a `serverOperation` as a job on the bundle's job server, and every
 * other action (or any action when the server can't be started or refuses the job) as
 * its command.
 */
export async function runActionPreferringServer(
    action: ActionSpec,
    context: CommandContext,
    signal: AbortSignal | undefined,
    bundleRoot: string,
    runProcess: RunProcess,
    jobServer: JobServer | undefined,
    emit: ActionEmit = () => { },
): Promise<ActionRunResult> {
    const operation = action.serverOperation;
    if (operation && jobServer && !signal?.aborted) {
        const parameters = renderedParameters(operation, await contextWithFileState(context, bundleRoot));
        const job = await startServerJob(jobServer, operation.operation, parameters);
        if (job) {
            return runJob(job.address, job.id, `${operation.operation} ${JSON.stringify(parameters)}`, signal, emit);
        }
    }
    return runAction(action, context, signal, bundleRoot, runProcess, emit);
}

/** Starts the job, restarting a server that stopped answering once; undefined when no job was created. */
async function startServerJob(jobServer: JobServer, operation: string, parameters: LooseRecord): Promise<{ address: JobServerAddress; id: string } | undefined> {
    for (let attempt = 0; attempt < 2; attempt += 1) {
        const address = await jobServer.address();
        if (!address) {
            return undefined;
        }
        const started = await startJob(address, operation, parameters);
        if (started.started) {
            return { address, id: started.job.id };
        }
        console.warn(`Job server did not start '${operation}': ${started.reason}`);
        if (!started.unreachable) {
            return undefined;
        }
        jobServer.stop();
    }
    return undefined;
}

/** String values are interpolated, and left out when that leaves them empty; other values are sent as written. */
export function renderedParameters(operation: ServerOperationSpec, context: CommandContext): LooseRecord {
    const parameters: LooseRecord = {};
    for (const [key, value] of Object.entries(operation.parameters ?? {})) {
        if (typeof value !== "string") {
            parameters[key] = value;
            continue;
        }
        const rendered = interpolate(value, context).trim();
        if (rendered) {
            parameters[key] = rendered;
        }
    }
    return parameters;
}

async function runJob(address: JobServerAddress, id: string, command: string, signal: AbortSignal | undefined, emit: ActionEmit): Promise<ActionRunResult> {
    const startedAt = new Date().toISOString();
    await emit({ type: "start", command, startedAt });
    const stdout = new TailBuffer(maxOutputBytes);
    const write = (text: string) => {
        stdout.push(Buffer.from(text));
        void emit({ type: "output", stream: "stdout", text });
    };
    const stopFollowing = new AbortController();
    let cancelled = false;
    let cancelTimer: NodeJS.Timeout | undefined;
    const onAbort = () => {
        cancelled = true;
        cancelJob(address, id).catch((error: unknown) => {
            console.warn(`Could not cancel job ${id}: ${errorMessage(error)}`);
            stopFollowing.abort();
        });
        cancelTimer = setTimeout(() => stopFollowing.abort(), cancelWaitMs);
    };
    if (signal?.aborted) {
        onAbort();
    }
    else {
        signal?.addEventListener("abort", onAbort, { once: true });
    }
    try {
        await followJob(address, id, (event) => {
            const line = eventLine(event);
            if (line) {
                write(`${line}\n`);
            }
        }, stopFollowing.signal).catch((error: unknown) => {
            if (!stopFollowing.signal.aborted) {
                throw error;
            }
        });
    }
    finally {
        signal?.removeEventListener("abort", onAbort);
        clearTimeout(cancelTimer);
    }
    if (cancelled) {
        throw new Error("Process cancelled.");
    }
    const job = await getJob(address, id);
    const log = await jobLog(address, id);
    if (log) {
        write(log.endsWith("\n") ? log : `${log}\n`);
    }
    const stderr = job.error == null ? "" : `${errorText(job.error)}\n`;
    if (stderr) {
        void emit({ type: "output", stream: "stderr", text: stderr });
    }
    const result: ActionRunResult = {
        exitCode: typeof job.exit_code === "number" ? job.exit_code : job.status === "succeeded" ? 0 : 1,
        signal: null,
        stdout: stdout.text(),
        stderr,
        stdoutTruncated: stdout.truncated,
        startedAt,
        finishedAt: new Date().toISOString(),
        command,
    };
    await emit({ type: "complete", result });
    return result;
}

function eventLine(event: JobEvent): string | undefined {
    const { data } = event;
    if (event.type === "status" && typeof data.status === "string") {
        return `[job] ${data.status}`;
    }
    if (event.type === "progress") {
        const fraction = typeof data.fraction === "number" ? ` (${Math.round(data.fraction * 100)}%)` : "";
        const label = [data.stage, data.message].filter((part) => typeof part === "string" && part).join(": ");
        return label || fraction ? `[progress] ${label}${fraction}` : undefined;
    }
    return undefined;
}

function errorText(error: unknown): string {
    if (error && typeof error === "object" && "message" in error) {
        return String(error.message);
    }
    return typeof error === "string" ? error : JSON.stringify(error);
}
