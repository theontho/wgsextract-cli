import { escapeAttribute, escapeHTML } from "../dom.js";
import { isUpdateButtonVisible } from "../tauri-updater.js";
import { state } from "../state.js";

export function renderUpdateNavigationItem() {
    if (!isUpdateButtonVisible()) {
        return "";
    }
    const update = state.update;
    const percent = normalizedPercent(update.percent);
    const percentLabel = percent == null ? "" : `${Math.round(percent)}%`;
    const statusClass = escapeAttribute(update.status);
    return `
      <div class="update-nav-anchor" data-update-surface>
        <button class="nav-item update-nav-item ${statusClass}" type="button" data-update-toggle aria-expanded="${update.popoverVisible ? "true" : "false"}" aria-controls="update-popover" aria-haspopup="dialog">
          <span class="nav-icon update-progress-icon" style="--update-progress: ${percent ?? 0}">
            <span class="update-progress-ring" aria-hidden="true"></span>
            <span class="update-progress-symbol" aria-hidden="true">↻</span>
          </span>
          <span class="update-nav-copy">
            <span>${escapeHTML(updateButtonTitle())}</span>
            ${percentLabel && update.status === "downloading" ? `<span>${escapeHTML(percentLabel)}</span>` : ""}
          </span>
        </button>
      </div>
    `;
}

export function renderUpdatePopover() {
    if (!isUpdateButtonVisible() || !state.update.popoverVisible) {
        return "";
    }
    const percent = normalizedPercent(state.update.percent);
    const percentLabel = percent == null ? "" : `${Math.round(percent)}%`;
    const update = state.update;
    const heading = updatePopoverHeading();
    const showVersionList = Boolean(update.availableVersion);
    const versionList = showVersionList
        ? `
        <dl class="update-version-list">
          <div><dt>Current</dt><dd>${escapeHTML(update.currentVersion || state.applicationVersion || "Unknown")}</dd></div>
          <div><dt>New</dt><dd>${escapeHTML(update.availableVersion || "Unknown")}</dd></div>
        </dl>`
        : "";
    return `
      <section id="update-popover" class="update-popover" data-update-popover data-update-surface role="dialog" aria-label="${escapeAttribute(heading)}" tabindex="-1">
        <header>
          <strong>${escapeHTML(heading)}</strong>
          <span>${escapeHTML(updateStatusSummary())}</span>
        </header>${versionList}
        ${renderUpdateProgress(percentLabel)}
        ${update.message ? `<p class="update-message">${escapeHTML(update.message)}</p>` : ""}
        ${renderUpdateAction()}
      </section>
    `;
}

function renderUpdateProgress(percentLabel: string) {
    const update = state.update;
    if (!["downloading", "downloaded", "installing"].includes(update.status)) {
        return "";
    }
    const percent = normalizedPercent(update.percent) ?? 0;
    return `
      <div class="update-progress-row" aria-label="Download progress">
        <div class="update-progress-track"><span style="width: ${percent}%"></span></div>
        <span>${escapeHTML(percentLabel || `${Math.round(percent)}%`)}</span>
      </div>
    `;
}

function renderUpdateAction() {
    switch (state.update.status) {
        case "available":
            return `<button type="button" class="update-primary-action" data-update-download>Download</button>`;
        case "error":
            // If download already completed but install failed, offer retry install rather than re-download.
            if (state.update.bytesRid != null) {
                return `<button type="button" class="update-primary-action" data-update-install>Retry install</button>`;
            }
            return `<button type="button" class="update-primary-action" data-update-download>Download</button>`;
        case "downloading":
            return `<button type="button" class="update-primary-action" disabled>Downloading...</button>`;
        case "downloaded":
            return `<button type="button" class="update-primary-action" data-update-install>Install update</button>`;
        case "installing":
            return `<button type="button" class="update-primary-action" disabled>Starting installer...</button>`;
        default:
            return "";
    }
}

function updateButtonTitle() {
    switch (state.update.status) {
        case "downloading":
            return "Downloading update";
        case "checking":
            return "Checking for updates";
        case "downloaded":
            return "Install update";
        case "installing":
            return "Installing update";
        case "error":
            return state.update.bytesRid != null ? "Install failed" : "Update check failed";
        default:
            return "Update available";
    }
}

function updatePopoverHeading() {
    switch (state.update.status) {
        case "checking":
            return "Checking for updates";
        case "error":
            return state.update.bytesRid != null ? "Install failed" : "Update check failed";
        default:
            return "Update available";
    }
}

function updateStatusSummary() {
    switch (state.update.status) {
        case "downloading":
            return "Downloading";
        case "checking":
            return "Checking";
        case "downloaded":
            return "Ready to install";
        case "installing":
            return "Installer starting";
        case "error":
            return state.update.bytesRid != null ? "Installer did not complete" : "Could not reach the update server";
        default:
            return `${state.update.currentVersion || state.applicationVersion || "Current"} -> ${state.update.availableVersion || "new version"}`;
    }
}

function normalizedPercent(value: unknown) {
    return typeof value === "number" && Number.isFinite(value)
        ? Math.max(0, Math.min(100, value))
        : null;
}
