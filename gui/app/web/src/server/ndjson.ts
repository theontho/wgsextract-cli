import type { ServerResponse } from "node:http";
import type { LooseRecord } from "../../../shared/types.js";
import { asError, errorMessage } from "./errors.js";

export type EmitEvent = (event: LooseRecord) => Promise<void>;

/**
 * Streams events as NDJSON. The run is aborted when the client disconnects or a write fails, and a
 * failure is reported as a final `failureEvent` while the response is still writable.
 */
export async function streamNDJSON(
    response: ServerResponse,
    run: (emit: EmitEvent, signal: AbortSignal) => Promise<unknown>,
    failureEvent: (message: string) => LooseRecord,
) {
    const abortController = new AbortController();
    response.on("close", () => {
        if (!response.writableEnded) {
            abortController.abort();
        }
    });
    response.writeHead(200, { "content-type": "application/x-ndjson; charset=utf-8", "cache-control": "no-store" });
    let writeQueue = Promise.resolve();
    let writeError: unknown;
    // Never rejects: the first write failure is recorded and cancels the run.
    const emit: EmitEvent = (event) => {
        writeQueue = writeQueue
            .then(() => (writeError == null ? writeNDJSON(response, event) : undefined))
            .catch((error: unknown) => {
                writeError ??= error;
                abortController.abort();
            });
        return writeQueue;
    };
    try {
        await run(emit, abortController.signal);
        await writeQueue;
        if (writeError != null) {
            throw writeError;
        }
        response.end();
    }
    catch (error) {
        if (response.destroyed || response.writableEnded) {
            return;
        }
        try {
            await writeQueue;
            if (writeError != null) {
                throw writeError;
            }
            await writeNDJSON(response, failureEvent(errorMessage(error)));
            response.end();
        }
        catch (_writeError) {
            response.destroy(asError(error));
        }
    }
}

async function writeNDJSON(response: ServerResponse, event: LooseRecord) {
    if (response.write(`${JSON.stringify(event)}\n`)) {
        return;
    }
    await new Promise<void>((resolve, reject) => {
        const cleanup = () => {
            response.off("drain", onDrain);
            response.off("error", onError);
        };
        const onDrain = () => {
            cleanup();
            resolve();
        };
        const onError = (error: Error) => {
            cleanup();
            reject(error);
        };
        response.once("drain", onDrain);
        response.once("error", onError);
    });
}
