import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import type { LooseRecord } from "../../../shared/types.js";
import { errorMessage } from "./errors.js";

const ENV_VAR = "GUI_FOR_CLI_SETUP_EVENTS_LOG";
const initialized = new Set<string>();

export function wrapEmitWithEventLog<Result>(emit: (event: LooseRecord) => Result): (event: LooseRecord) => Result {
    const logPath = process.env[ENV_VAR];
    if (!logPath) {
        return emit;
    }
    return (event) => {
        appendEventToLog(logPath, event);
        return emit(event);
    };
}

function appendEventToLog(logPath: string, event: LooseRecord) {
    try {
        if (!initialized.has(logPath)) {
            mkdirSync(path.dirname(logPath), { recursive: true });
            writeFileSync(logPath, "");
            initialized.add(logPath);
        }
        appendFileSync(logPath, `${JSON.stringify(event)}\n`);
    }
    catch (error) {
        console.warn(`Could not write setup event log '${logPath}': ${errorMessage(error)}`);
    }
}
