import type { LooseRecord } from "../../../shared/types.js";
import { errorMessage } from "./errors.js";

/** Where a running job server listens, as it announced on stdout. */
export interface JobServerAddress {
    url: string;
    token: string;
}

/** A job record; servers may add fields. `exit_code` is set once the job has finished. */
export interface JobRecord extends LooseRecord {
    id: string;
    status: string;
    exit_code?: number | null;
    error?: unknown;
}

export interface JobEvent {
    type: string;
    data: LooseRecord;
}

export type StartJobResult =
    | { started: true; job: JobRecord }
    | { started: false; reason: string; unreachable: boolean };

/** Starts `operation` as a job. A refused or failed request reports why; no job was created then. */
export async function startJob(address: JobServerAddress, operation: string, parameters: LooseRecord): Promise<StartJobResult> {
    let response: Response;
    try {
        response = await request(address, "POST", `/ops/${operation}`, parameters);
    }
    catch (error) {
        return { started: false, reason: errorMessage(error), unreachable: true };
    }
    const body = await response.json().catch(() => undefined) as LooseRecord | undefined;
    if (!response.ok || !isJobRecord(body)) {
        return { started: false, reason: `HTTP ${response.status}: ${describeError(body)}`, unreachable: false };
    }
    return { started: true, job: body };
}

/** Reads the job's server-sent events until the server ends the stream, which it does once the job has finished. */
export async function followJob(address: JobServerAddress, id: string, onEvent: (event: JobEvent) => void, signal?: AbortSignal): Promise<void> {
    const response = await request(address, "GET", `/jobs/${encodeURIComponent(id)}/events`, undefined, signal);
    if (!response.ok || !response.body) {
        throw new Error(`Job events for ${id}: HTTP ${response.status}`);
    }
    let buffer = "";
    for await (const chunk of response.body.pipeThrough(new TextDecoderStream())) {
        buffer = (buffer + chunk).replaceAll("\r\n", "\n");
        let end: number;
        while ((end = buffer.indexOf("\n\n")) >= 0) {
            const event = parseEvent(buffer.slice(0, end));
            buffer = buffer.slice(end + 2);
            if (event) {
                onEvent(event);
            }
        }
    }
}

export async function cancelJob(address: JobServerAddress, id: string): Promise<void> {
    const response = await request(address, "DELETE", `/jobs/${encodeURIComponent(id)}`);
    // 409: it had already finished.
    if (!response.ok && response.status !== 409) {
        throw new Error(`Cancelling job ${id}: HTTP ${response.status}`);
    }
}

export async function getJob(address: JobServerAddress, id: string): Promise<JobRecord> {
    const response = await request(address, "GET", `/jobs/${encodeURIComponent(id)}`);
    const body = await response.json().catch(() => undefined) as LooseRecord | undefined;
    if (!response.ok || !isJobRecord(body)) {
        throw new Error(`Job ${id}: HTTP ${response.status}: ${describeError(body)}`);
    }
    return body;
}

/** The job's whole log, or "" when the server has none. */
export async function jobLog(address: JobServerAddress, id: string): Promise<string> {
    const response = await request(address, "GET", `/jobs/${encodeURIComponent(id)}/log?tail=0`);
    return response.ok ? response.text() : "";
}

function request(address: JobServerAddress, method: string, path: string, body?: LooseRecord, signal?: AbortSignal): Promise<Response> {
    return fetch(`${address.url}${path}`, {
        method,
        headers: {
            authorization: `Bearer ${address.token}`,
            ...(body ? { "content-type": "application/json" } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
        ...(signal ? { signal } : {}),
    });
}

function parseEvent(block: string): JobEvent | undefined {
    let type = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
        if (line.startsWith("event:")) {
            type = line.slice(6).trim();
        }
        else if (line.startsWith("data:")) {
            data.push(line.slice(5).trimStart());
        }
    }
    if (data.length === 0) {
        return undefined;
    }
    try {
        const parsed: unknown = JSON.parse(data.join("\n"));
        return parsed && typeof parsed === "object" ? { type, data: parsed as LooseRecord } : undefined;
    }
    catch {
        return undefined;
    }
}

function isJobRecord(value: LooseRecord | undefined): value is JobRecord {
    return typeof value?.id === "string" && typeof value.status === "string";
}

function describeError(body: LooseRecord | undefined): string {
    const error = body?.error;
    if (error && typeof error === "object" && "message" in error) {
        return String(error.message);
    }
    return "no job in the response";
}
