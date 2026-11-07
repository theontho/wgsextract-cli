import { escapeHTML } from "../dom.js";
import { formatLabel, renderIcon } from "../model.js";
import { ensureSetupPreflight, setupInstallSizeGB } from "../operations.js";
import { state } from "../state.js";
import { setupToolSummary } from "./setup-tool-summary.js";
import { setupStepsForPlatform } from "../../../../shared/setup-platforms.js";

export function setupNeedsAttention() {
    const status = state.setupRun?.status;
    return setupSteps().length > 0 && status !== "ok" && status !== "warning";
}

export function setupHasNeverRun() {
    return setupSteps().length > 0 && !state.setupRun;
}

export function setupPageID() {
    return state.manifest?.pages?.find((page) => page.id === "settings")?.id ?? state.manifest?.pages?.[0]?.id ?? "";
}

export function setupPromptMessage() {
    const body = setupPromptBody();
    const toolSummary = setupPromptToolSummary();
    return toolSummary ? `${body}\n\n${toolSummary}` : body;
}
function setupPromptBody() {
    const appName = state.manifest?.displayName?.trim() ||
        state.labels.setupPromptAppNameFallback ||
        "This app";
    return formatLabel(state.labels.setupPromptBodyFormat ||
        "Do you want to run setup? %{app} will probably not work properly without running setup.", { app: appName });
}
function setupPromptToolSummary() {
    return setupSteps().map((step) => setupToolSummary(step, state.labels)).find(Boolean);
}
function setupInitialInstallSizeMessage() {
    const sizeGB = setupInstallSizeGB();
    if (!sizeGB) {
        return "";
    }
    return formatLabel(state.labels.setupInitialInstallSizeFormat ||
        "Initial setup will install about %{size} GB.", { size: formatSetupGB(sizeGB) });
}
function setupDiskSpaceMessage() {
    if (!setupInstallSizeGB()) {
        return "";
    }
    ensureSetupPreflight();
    if (state.loadingSetupPreflight) {
        return state.labels.setupDiskSpaceCheckingTitle ?? "Checking available disk space...";
    }
    if (state.setupPreflightError) {
        return formatLabel(state.labels.setupDiskSpaceCheckFailedFormat ||
            "Could not check available disk space: %{error}", { error: state.setupPreflightError });
    }
    return state.setupPreflight?.message ?? "";
}
function setupDiskSpaceClass() {
    if (state.setupPreflight?.severity === "warning") {
        return " warning";
    }
    if (state.setupPreflightError) {
        return " warning";
    }
    return "";
}
function setupRunDisabled() {
    return state.loadingSetupPreflight || state.setupPreflight?.severity === "warning";
}
function formatSetupGB(value: number) {
    return Number.isInteger(value) ? String(value) : value.toFixed(value >= 10 ? 1 : 2);
}

function setupSteps() {
    return setupStepsForPlatform(state.manifest?.setup?.steps ?? []);
}

export function renderSetupGlobalStatusBar() {
    if (!setupNeedsAttention()) {
        return "";
    }
    const isRunning = state.setupRun?.status === "running";
    const title = setupGlobalStatusTitle();
    const message = setupGlobalStatusMessage();
    return `
      <button type="button" class="setup-global-banner" data-setup-global-start>
        <span class="setup-global-icon" aria-hidden="true">${isRunning ? `<span class="mini-spinner"></span>` : "⚠"}</span>
        <span class="setup-global-copy">
          <strong>${escapeHTML(title)}</strong>
          <span>${escapeHTML(message)}</span>
        </span>
        <span class="setup-global-action">${escapeHTML(isRunning ? state.labels.setupRunningTitle ?? "Running setup..." : state.labels.setupRunButtonTitle ?? "Run Setup")}</span>
      </button>
    `;
}

export function renderSetupPromptDialog() {
    if (!state.setupPromptVisible) {
        return "";
    }
    const body = setupPromptBody();
    const toolSummary = setupPromptToolSummary();
    const sizeMessage = setupInitialInstallSizeMessage();
    const diskSpaceMessage = setupDiskSpaceMessage();
    const disabled = setupRunDisabled() ? " disabled" : "";
    return `
      <div class="modal-backdrop" role="presentation">
        <section class="confirmation-modal setup-prompt-modal" role="alertdialog" aria-modal="true" aria-labelledby="setup-prompt-title">
          <h2 id="setup-prompt-title">${escapeHTML(state.labels.setupTitle ?? "Setup")}</h2>
          <p>${escapeHTML(body)}</p>
          ${sizeMessage ? `<p class="setup-prompt-size">${escapeHTML(sizeMessage)}</p>` : ""}
          ${diskSpaceMessage ? `<p class="setup-prompt-disk${setupDiskSpaceClass()}">${escapeHTML(diskSpaceMessage)}</p>` : ""}
          ${toolSummary ? `<p class="setup-prompt-tool">${escapeHTML(toolSummary)}</p>` : ""}
          <div class="modal-actions">
            <button type="button" class="secondary-button" data-setup-prompt-dismiss autofocus>${escapeHTML(state.labels.terminalCancelButtonTitle ?? "Cancel")}</button>
            <button type="button" class="action-button primary" data-setup-prompt-run${disabled}>${renderIcon("play.fill", undefined, "▶")}${escapeHTML(state.labels.setupRunButtonTitle ?? "Run Setup")}</button>
          </div>
        </section>
      </div>
    `;
}

function setupGlobalStatusTitle() {
    switch (state.setupRun?.status) {
        case "running":
            return state.labels.setupRunningTitle ?? "Running setup...";
        case "failed":
            return state.labels.setupStatusFailedTitle ?? "Setup failed. Review command output for details.";
        default:
            return state.labels.setupTitle ?? "Setup";
    }
}

function setupGlobalStatusMessage() {
    switch (state.setupRun?.status) {
        case "running":
            return state.labels.setupRunningTitle ?? "Running setup...";
        case "failed":
            return state.labels.setupStatusFailedTitle ?? "Setup failed. Review command output for details.";
        default:
            return state.labels.setupStatusReadyTitle ?? "Review and run this bundle's setup steps.";
    }
}
