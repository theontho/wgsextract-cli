import { setupResultLine } from "../../../shared/rendering.js";
import { api, apiStream } from "./api.js";
import { errorMessage } from "./model.js";
import { scheduleRender } from "./rerender.js";
import { state } from "./state.js";
import { appendTerminal, appendTerminalOutput, runningActionControllers } from "./terminal.js";
import { persistBundleState } from "./operations-state.js";
import { resolveSetupPreflight } from "./operations-setup-preflight.js";
import type { SetupRun, SetupStepResult, TerminalEntry } from "../../../shared/types.js";

let setupElapsedTimer: ReturnType<typeof setInterval> | null = null;
let runningSetup: AbortController | null = null;

export async function runSetup() {
    if (state.setupRun?.status === "running") {
        return;
    }
    const preflight = await resolveSetupPreflight();
    if (preflight?.severity === "warning") {
        const setupID = appendTerminal("error", preflight.title ?? state.labels.setupTitle ?? "Setup", preflight.message ?? "");
        state.activeTerminalID = setupID;
        state.setupRun = {
            status: "failed",
            results: [],
            ...(preflight.message != null ? { error: preflight.message } : {}),
            completedAt: new Date().toISOString(),
        };
        await persistBundleState();
        scheduleRender();
        return;
    }
    const setupID = appendTerminal("command", state.labels.setupTitle ?? "Setup", state.labels.setupRunningTitle ?? "Running setup...");
    state.activeTerminalID = setupID;
    clearSetupElapsedTimer();
    state.setupRun = { status: "running", results: [], currentStepID: null, currentStepStartedAt: null, currentStepElapsedMs: 0 };
    // Closing the setup tab (or cancelling) aborts the stream, which stops the server-side setup run.
    const controller = new AbortController();
    runningSetup = controller;
    runningActionControllers.set(setupID, controller);
    scheduleRender();
    const entry = () => state.terminalEntries.find((candidate) => candidate.id === setupID);
    try {
        await apiStream<SetupStreamEvent>(
            "/api/setup/stream",
            { locale: state.localizationCode },
            (event) => applySetupEvent(event, entry()),
            { signal: controller.signal, label: "Setup stream" },
        );
        if (state.setupRun?.status && state.setupRun.status !== "running") {
            await persistBundleState();
        }
    }
    catch (error) {
        clearSetupElapsedTimer();
        const message = controller.signal.aborted ? "Setup cancelled." : errorMessage(error);
        const tab = entry();
        if (tab) {
            tab.kind = "error";
            tab.body = [tab.body, message].filter(Boolean).join("\n");
        }
        state.setupRun = { status: "failed", results: state.setupRun?.results ?? [], error: message, completedAt: new Date().toISOString() };
        await persistBundleState();
    }
    finally {
        runningActionControllers.delete(setupID);
        runningSetup = null;
        // Results from before setup, errors included, may no longer hold now that its tools are installed.
        state.dataSourcePayloads.clear();
        state.dataSourceErrors.clear();
    }
    scheduleRender();
}
/** Cancels the running setup, if any; the server terminates the current step's process tree. */
export function cancelSetup() {
    runningSetup?.abort();
}
export async function openBundleWorkspace() {
    await api<void>("/api/open-bundle-workspace", { method: "POST", body: {} });
}
type SetupStreamEvent =
    | { type: "step-start"; step: { id: string; label?: string; command?: string } }
    | { type: "output"; text?: string }
    | { type: "step-complete"; result: SetupStepResult & { durationMs?: number } }
    | { type: "complete"; result: SetupRun };

function applySetupEvent(event: SetupStreamEvent, tab: TerminalEntry | undefined) {
    if (!tab) {
        return;
    }
    switch (event.type) {
        case "step-start":
            startSetupElapsedTimer(event.step.id);
            state.setupRun = {
                ...(state.setupRun ?? {}),
                status: "running",
                currentStepID: event.step.id,
                currentStepStartedAt: new Date().toISOString(),
                currentStepElapsedMs: 0,
            };
            tab.body = [tab.body, `==> ${event.step.label}`, `$ ${event.step.command}\n`].filter(Boolean).join("\n");
            break;
        case "output":
            appendTerminalOutput(tab.id, event.text ?? "");
            return;
        case "step-complete":
            clearSetupElapsedTimer();
            state.setupRun = {
                ...(state.setupRun ?? {}),
                status: "running",
                currentStepID: null,
                currentStepStartedAt: null,
                currentStepElapsedMs: event.result.durationMs ?? 0,
                results: [
                    ...(state.setupRun?.results ?? []).filter((result) => result.id !== event.result.id),
                    event.result,
                ],
            };
            tab.body = [tab.body, setupResultLine(event.result)].filter(Boolean).join("\n");
            break;
        case "complete":
            clearSetupElapsedTimer();
            state.setupRun = { ...event.result, completedAt: new Date().toISOString(), currentStepID: null, currentStepStartedAt: null, currentStepElapsedMs: 0 };
            tab.kind = event.result?.status === "failed" ? "error" : "success";
            if (event.result?.error) {
                tab.body = [tab.body, event.result.error].filter(Boolean).join("\n");
            }
            break;
    }
    scheduleRender();
}

function startSetupElapsedTimer(stepID: string) {
    clearSetupElapsedTimer();
    const startedAtMs = Date.now();
    setupElapsedTimer = setInterval(() => {
        if (state.setupRun?.status !== "running" || state.setupRun.currentStepID !== stepID) {
            clearSetupElapsedTimer();
            return;
        }
        state.setupRun = {
            ...state.setupRun,
            currentStepElapsedMs: Date.now() - startedAtMs,
        };
        scheduleRender();
    }, 1000);
}

function clearSetupElapsedTimer() {
    if (setupElapsedTimer == null) {
        return;
    }
    clearInterval(setupElapsedTimer);
    setupElapsedTimer = null;
}
