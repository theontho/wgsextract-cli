import { contextValue, displayCommand, placeholdersIn } from "../../../shared/rendering.js";
import { errorMessage } from "./model.js";
import { scheduleRender } from "./rerender.js";
import { state } from "./state.js";
import { apiStream } from "./api.js";
import { appendTerminal, appendTerminalOutput, runningActionControllers, terminalExitStatus, terminalProcessErrorStatus } from "./terminal.js";
import type { ActionReference, ActionSpec, CommandContext, CommandSpec, ValueMap } from "../../../shared/types.js";

type ActionStreamEvent =
    | { type: "start"; command: string }
    | { type: "output"; text?: string }
    | { type: "complete"; result: { exitCode: number; command: string } }
    | { type: "error"; error?: string };

export async function runAction(action: ActionSpec, context: CommandContext, reference: ActionReference) {
    if (action.confirm) {
        state.pendingConfirmation = { action, reference, context, input: "" };
        scheduleRender();
        return;
    }
    if (!action.command) {
        throw new Error("Missing action command.");
    }
    const command = displayCommand(action.command, context);
    const runningID = appendTerminal(
        "command",
        action.title,
        actionTerminalBody(action.title, context, action),
        command);
    const controller = new AbortController();
    runningActionControllers.set(runningID, controller);
    scheduleRender();
    try {
        await apiStream<ActionStreamEvent>(
            "/api/run/stream",
            { action: reference, context },
            (event) => applyActionEvent(event, action, context, runningID),
            { signal: controller.signal, label: "Action stream" },
        );
    }
    catch (error) {
        if (error && typeof error === "object" && "name" in error && error.name === "AbortError") {
            return;
        }
        const runningIndex = state.terminalEntries.findIndex((entry) => entry.id === runningID);
        if (runningIndex < 0) {
            return;
        }
        const existing = state.terminalEntries[runningIndex];
        if (!existing) {
            return;
        }
        const failedCommand = existing.command || command;
        state.terminalEntries[runningIndex] = {
            ...existing,
            kind: "error",
            title: action.title ?? "Action",
            command: failedCommand,
            body: [
                existing.body || actionExecutionLine(action.title, context, action),
                errorMessage(error),
            ].join("\n"),
            status: terminalProcessErrorStatus(failedCommand, errorMessage(error)),
        };
    }
    finally {
        runningActionControllers.delete(runningID);
        state.dataSourcePayloads.clear();
        scheduleRender();
    }
}

function applyActionEvent(event: ActionStreamEvent, action: ActionSpec, context: CommandContext, runningID: string) {
    const tab = state.terminalEntries.find((entry) => entry.id === runningID);
    if (!tab) {
        return;
    }
    switch (event.type) {
        case "start":
            tab.command = event.command;
            tab.body = [
                `$ ${event.command}`,
                actionExecutionLine(action.title, context, action),
                "[running] Started.",
            ].join("\n") + "\n";
            break;
        case "output":
            appendTerminalOutput(runningID, event.text ?? "");
            return;
        case "complete": {
            const result = event.result;
            const status = result.exitCode === 0 ? undefined : terminalExitStatus(result.exitCode, result.command);
            tab.kind = result.exitCode === 0 ? "success" : status?.severity ?? "error";
            tab.command = result.command;
            if (status) {
                tab.status = status;
            }
            else {
                delete tab.status;
            }
            tab.body = [tab.body, `exit ${result.exitCode}`].filter(Boolean).join("\n");
            break;
        }
        case "error":
            throw new Error(event.error ?? "Action failed.");
    }
    scheduleRender();
}

function actionTerminalBody(title: unknown, context: CommandContext, action: ActionSpec | null = null) {
    return [
        actionExecutionLine(title, context, action),
        "[queued] Preparing command environment...",
    ].join("\n");
}
function actionExecutionLine(title: unknown, context: CommandContext, action: ActionSpec | null = null) {
    return `[action] Executing action "${title}" with inputs ${actionInputSummary(context, action)}`;
}
function actionInputSummary(context: CommandContext, action: ActionSpec | null = null) {
    const entries: string[] = [];
    const seen = new Set<string>();
    if (action?.command) {
        for (const placeholder of actionCommandPlaceholders(action.command)) {
            addPlaceholderInputEntry(entries, seen, placeholder, context);
        }
    }
    else {
        addInputEntries(entries, seen, context.fieldValues, context);
        addInputEntries(entries, seen, context.checkedOptions, context);
        addInputEntries(entries, seen, context.rowValues, context);
        addInputEntries(entries, seen, context.configValues, context);
    }
    return entries.length ? entries.join(", ") : "(none)";
}
function actionCommandPlaceholders(command: CommandSpec) {
    return placeholdersIn([
        command.executable,
        ...(command.arguments ?? []),
        ...(command.optionalArguments ?? []).flat(),
    ]).filter((placeholder) => !["bundleRoot", "bundleWorkspace", "home"].includes(placeholder));
}
function addPlaceholderInputEntry(entries: string[], seen: Set<string>, placeholder: string, context: CommandContext) {
    const key = inputValueKey(placeholder);
    const rawValue = key === placeholder ? contextValue(context, placeholder) : contextValue(context, key);
    const text = String(rawValue ?? "").trim();
    if (!text) {
        return;
    }
    const label = inputLabel(key, context);
    const displayValue = displayInputValue(key, label, text);
    const dedupeKey = `${normalizedInputLabelKey(key)}\u0000${label}\u0000${displayValue}`;
    if (seen.has(dedupeKey)) {
        return;
    }
    seen.add(dedupeKey);
    entries.push(`${label}=${displayValue}`);
}
function addInputEntries(entries: string[], seen: Set<string>, values: ValueMap | undefined, context: CommandContext) {
    for (const [key, value] of Object.entries(values ?? {})) {
        const text = String(value ?? "").trim();
        const label = inputLabel(key, context);
        const displayValue = displayInputValue(key, label, text);
        const dedupeKey = `${normalizedInputLabelKey(key)}\u0000${label}\u0000${displayValue}`;
        if (!text || seen.has(dedupeKey)) {
            continue;
        }
        seen.add(dedupeKey);
        entries.push(`${label}=${displayValue}`);
    }
}
function displayInputValue(key: string, label: string, value: string) {
    return isSensitiveInput(key, label) ? "<redacted>" : value;
}
function isSensitiveInput(key: string, label: string) {
    const haystack = `${normalizedInputLabelKey(key)} ${label}`.toLowerCase();
    return ["token", "secret", "password", "passphrase", "api_key", "apikey", "api key", "private_key", "private key"].some((marker) => haystack.includes(marker));
}
function inputValueKey(placeholder: string) {
    const separator = placeholder.lastIndexOf(".");
    if (separator < 0 || separator === placeholder.length - 1) {
        return placeholder;
    }
    const suffix = placeholder.slice(separator + 1);
    if (["exists", "fileSize", "fileSizeGB", "isIndexed", "isSorted", "pathExtension", "parentDir"].includes(suffix)) {
        return placeholder.slice(0, separator);
    }
    return placeholder;
}
function inputLabel(key: string, context: CommandContext) {
    return String(context.placeholderLabels?.[normalizedInputLabelKey(key)] ?? prettifyInputKey(key));
}
function normalizedInputLabelKey(key: string) {
    if (key.startsWith("row.")) {
        return key.slice(4);
    }
    if (key.startsWith("config.")) {
        return key.slice(7);
    }
    const separator = key.lastIndexOf(".");
    if (separator < 0 || separator === key.length - 1) {
        return key;
    }
    const suffix = key.slice(separator + 1);
    return suffix === "fileSize" || suffix === "fileSizeGB" ? key.slice(0, separator) : key;
}
function prettifyInputKey(key: string) {
    return key
        .replace(/^(row|config)\./, "")
        .replace(/[._-]+/g, " ")
        .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
