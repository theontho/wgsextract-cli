import { errorMessage } from "./errors.js";
import type { RunProcess } from "../../../shared/types.js";

type TimeoutState = { timedOut: boolean };

export function timeoutRunProcess(runProcess: RunProcess, timeoutSeconds: unknown, timeoutState?: TimeoutState): RunProcess {
    if (typeof timeoutSeconds !== "number" || !Number.isFinite(timeoutSeconds) || timeoutSeconds <= 0) {
        return runProcess;
    }
    return async (executable, args, options) => {
        try {
            return await runProcess(executable, args, {
                ...options,
                timeoutMs: effectiveTimeoutMs(options.timeoutMs, timeoutSeconds),
            });
        }
        catch (error) {
            if (/timed out/i.test(errorMessage(error))) {
                timeoutState && (timeoutState.timedOut = true);
            }
            throw error;
        }
    };
}

function effectiveTimeoutMs(existingTimeoutMs: number | undefined, stepTimeoutSeconds: number) {
    const stepTimeoutMs = stepTimeoutSeconds * 1000;
    if (typeof existingTimeoutMs === "number" && Number.isFinite(existingTimeoutMs) && existingTimeoutMs > 0) {
        return Math.min(existingTimeoutMs, stepTimeoutMs);
    }
    return stepTimeoutMs;
}
