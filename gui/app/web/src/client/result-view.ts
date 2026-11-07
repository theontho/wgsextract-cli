import type { ResultDocument, ResultFile, ResultNotice, ResultScalar } from "../../../shared/result-document.js";
import type { ActionReference, CommandContext } from "../../../shared/types.js";
import { escapeAttribute, escapeHTML } from "./dom.js";
import { state } from "./state.js";

/** Identifies where an action's notice shows: the action and, for a row action, its row. */
export function actionNoticeKey(reference: ActionReference, context: Pick<CommandContext, "rowValues">): string {
    return [reference.pageID, reference.sectionID, reference.controlID ?? "", reference.settingID ?? "", reference.actionID, JSON.stringify(context.rowValues ?? {})].join("\u0000");
}

/** The outputs, warnings and metrics of an action's result document, below its output in the terminal. */
export function renderResultSummary(document: ResultDocument | undefined): string {
    if (!document) {
        return "";
    }
    const labels = state.labels;
    const metrics = Object.entries(document.metrics);
    return `
    <section class="result-summary ${escapeAttribute(document.status)}" aria-label="${escapeAttribute(labels.resultTitle ?? "Result")}">
      <h3>${escapeHTML(labels.resultTitle ?? "Result")}: ${escapeHTML(statusLabel(document.status))}</h3>
      ${document.error?.message ? `<p class="result-error">${escapeHTML(document.error.message)}</p>` : ""}
      ${document.warnings.length ? `
        <h4>${escapeHTML(labels.resultWarningsTitle ?? "Warnings")}</h4>
        <ul class="result-warnings">
          ${document.warnings.map((warning) => `<li><span aria-hidden="true">⚠️</span> <code>${escapeHTML(warning.code)}</code> ${escapeHTML(warning.message)}</li>`).join("")}
        </ul>` : ""}
      ${metrics.length ? `
        <h4>${escapeHTML(labels.resultMetricsTitle ?? "Key metrics")}</h4>
        <dl class="result-metrics">
          ${metrics.map(([key, value]) => `<dt>${escapeHTML(key)}</dt><dd>${escapeHTML(formatScalar(value))}</dd>`).join("")}
        </dl>` : ""}
      ${document.outputs.length ? `
        <h4>${escapeHTML(labels.resultOutputsTitle ?? "Output files")}</h4>
        <ul class="result-outputs">${document.outputs.map(renderOutput).join("")}</ul>` : ""}
      ${document.manifestPath ? `
        <p class="result-manifest">${escapeHTML(labels.resultManifestLabel ?? "Result manifest")}: <span class="result-path">${escapeHTML(document.manifestPath)}</span> ${pathButtons(document.manifestPath)}</p>` : ""}
      <details class="result-document">
        <summary>${escapeHTML(labels.resultDocumentTitle ?? "Full result (JSON)")}</summary>
        <pre>${escapeHTML(JSON.stringify(document.raw, null, 2))}</pre>
      </details>
    </section>`;
}

/** What the last run of an action asks the user to do, shown next to its button. */
export function renderResultNotice(notice: ResultNotice | undefined): string {
    if (!notice) {
        return "";
    }
    const labels = state.labels;
    const title = notice.kind === "usage"
        ? labels.resultUsageTitle ?? "Check the selected options"
        : labels.resultMissingDependencyTitle ?? "A required tool is not installed";
    const setupButton = notice.kind === "missing_dependency"
        ? `<button type="button" class="action-button secondary compact" data-result-open-setup>${escapeHTML(labels.resultOpenSetupTitle ?? "Open Setup")}</button>`
        : "";
    return `
    <span class="precheck-banner warning result-notice" role="alert">
      <span aria-hidden="true">⚠️</span>
      <span><strong>${escapeHTML(title)}</strong><span>${escapeHTML(notice.message)}</span>${setupButton}</span>
    </span>`;
}

function renderOutput(output: ResultFile): string {
    const size = output.sizeBytes == null ? "" : ` <span class="result-size">${escapeHTML(formatBytes(output.sizeBytes))}</span>`;
    return `
      <li>
        <span class="result-role">${escapeHTML(output.role || output.kind)}</span>
        <span class="result-path" title="${escapeAttribute(output.path)}">${escapeHTML(output.path)}</span>${size}
        ${output.exists ? pathButtons(output.path) : ""}
      </li>`;
}

function pathButtons(filePath: string): string {
    const labels = state.labels;
    return `<span class="result-path-actions">
      <button type="button" class="action-button secondary compact" data-result-open="${escapeAttribute(filePath)}">${escapeHTML(labels.resultOpenTitle ?? "Open")}</button>
      <button type="button" class="action-button secondary compact" data-result-reveal="${escapeAttribute(filePath)}">${escapeHTML(labels.resultRevealTitle ?? "Show in Folder")}</button>
    </span>`;
}

function statusLabel(status: string): string {
    const labels = state.labels;
    switch (status) {
        case "ok":
            return labels.resultStatusOkTitle ?? "Completed";
        case "failed":
            return labels.resultStatusFailedTitle ?? "Failed";
        case "cancelled":
            return labels.resultStatusCancelledTitle ?? "Cancelled";
        default:
            return status;
    }
}

function formatScalar(value: ResultScalar): string {
    if (value === null) {
        return "—";
    }
    if (typeof value === "number" && !Number.isInteger(value)) {
        return String(Number(value.toPrecision(6)));
    }
    return String(value);
}

function formatBytes(bytes: number): string {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
    }
    return unit === 0 ? `${value} ${units[0]}` : `${value.toFixed(1)} ${units[unit]}`;
}
