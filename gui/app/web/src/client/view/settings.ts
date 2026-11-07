import { escapeAttribute, escapeHTML } from "../dom.js";
import { renderIcon, renderIconTitle } from "../model.js";
import { state } from "../state.js";
import { setupToolSummary } from "./setup-tool-summary.js";
import { formatDurationMs } from "../../../../shared/rendering.js";
import { setupStepsForPlatform } from "../../../../shared/setup-platforms.js";
import type { LocalizedOption, SetupRun, SetupStep, SetupStepResult } from "../../../../shared/types.js";

export function renderSetupStatusSection() {
  const steps = setupStepsForPlatform(state.manifest?.setup?.steps ?? []);
  const setupRun = state.setupRun ?? {};
  const resultsByID = new Map((setupRun.results ?? []).map((result) => [result.id, result]));
  const hasSteps = steps.length > 0;
  const toolSummary = hasSteps ? setupHeaderToolSummary(steps) : "";
  const isRunning = setupRun.status === "running";
  const runButtonTitle = setupRun.status === "ok" || setupRun.status === "warning"
    ? state.labels.setupRerunButtonTitle ?? "Rerun Setup"
    : state.labels.setupRunButtonTitle ?? "Run Setup";
  return `
    <section class="card setup-status-card">
      <div class="setup-status-header">
        <div>
          <h3>${escapeHTML(state.labels.setupTitle ?? "Setup")}</h3>
          <p class="muted">${escapeHTML(hasSteps ? setupStatusSummary(setupRun) : state.labels.setupNoStepsTitle ?? "No setup steps are defined for this bundle.")}</p>
          ${toolSummary ? `<p class="setup-header-tool">${escapeHTML(toolSummary)}</p>` : ""}
        </div>
        <div class="setup-actions">
          <button type="button" class="action-button secondary" data-open-bundle-workspace title="${escapeAttribute(state.labels.openBundleWorkspaceTooltip ?? "")}">${renderIcon("folder", undefined, "📁")}${escapeHTML(state.labels.openBundleWorkspaceTitle ?? "Open Bundle Workspace")}</button>
        ${hasSteps
          ? `<button type="button" class="action-button primary" data-run-setup ${isRunning ? "disabled" : ""}>${isRunning ? `<span class="mini-spinner" aria-hidden="true"></span>` : renderIcon("play.fill", undefined, "▶")}${escapeHTML(isRunning ? state.labels.setupRunningTitle ?? "Running setup..." : runButtonTitle)}</button>`
          : ""}
        ${isRunning
          ? `<button type="button" class="action-button secondary" data-cancel-setup>${escapeHTML(state.labels.terminalCancelButtonTitle ?? "Cancel")}</button>`
          : ""}
        </div>
      </div>
      ${hasSteps
        ? `<ol class="setup-step-list">
          ${steps.map((step) => renderSetupStepStatus(step, resultsByID.get(step.id), setupRun.currentStepID === step.id)).join("")}
        </ol>`
        : ""}
    </section>
  `;
}

function renderSetupStepStatus(step: SetupStep, result: SetupStepResult | undefined, isRunning: boolean) {
  const status = isRunning ? "running" : result?.status ?? "pending";
  const statusLabel = setupStatusLabel(status);
  const duration = formatDurationMs(isRunning ? state.setupRun?.currentStepElapsedMs : result?.durationMs);
  return `
    <li class="setup-step ${escapeAttribute(status)}">
      <span class="setup-step-status" aria-hidden="true">${setupStatusGlyph(status)}</span>
      <span class="setup-step-main">
        <span class="setup-step-title">${escapeHTML(step.label)}</span>
      </span>
      <span class="setup-step-kind">${escapeHTML(step.kind)}</span>
      <span class="setup-step-duration">${escapeHTML(duration)}</span>
      <span class="setup-step-label">${escapeHTML(statusLabel)}</span>
    </li>
  `;
}

function setupHeaderToolSummary(steps: SetupStep[]) {
  const summaries = steps.map((step) => setupToolSummary(step, state.labels)).filter(Boolean);
  return [...new Set(summaries)].join("; ");
}

function setupStatusSummary(setupRun: SetupRun) {
  switch (setupRun.status) {
    case "running":
      return state.labels.setupRunningTitle ?? "Running setup...";
    case "ok":
    case "warning":
      return state.labels.setupStatusOkTitle ?? "Setup completed successfully.";
    case "failed":
      return state.labels.setupStatusFailedTitle ?? "Setup failed. Review command output for details.";
    default:
      return state.labels.setupStatusReadyTitle ?? "Review and run this bundle's setup steps.";
  }
}

function setupStatusLabel(status: string) {
  switch (status) {
    case "running":
      return state.labels.setupStepRunningTitle ?? "Running";
    case "ok":
      return state.labels.setupStepOkTitle ?? "OK";
    case "warning":
      return state.labels.setupStepWarningTitle ?? "Warning";
    case "failed":
      return state.labels.setupStepFailedTitle ?? "Failed";
    default:
      return state.labels.setupStepPendingTitle ?? "Pending";
  }
}

function setupStatusGlyph(status: string) {
  switch (status) {
    case "running":
      return `<span class="mini-spinner" aria-hidden="true"></span>`;
    case "ok":
      return "✓";
    case "warning":
      return "!";
    case "failed":
      return "×";
    default:
      return "○";
  }
}

export function renderStandardOptionsAccessory() {
  const currentOption = state.localizationOptions.find((option) => option.code === state.localizationCode);
  const currentName = currentOption ? languageOptionLabel(currentOption) : state.localizationCode;
  const systemLabel = currentName
    ? `${state.labels.languageSystemDefaultLabel ?? "Use system default"} — ${currentName}`
    : state.labels.languageSystemDefaultLabel ?? "Use system default";
  return `
    <section class="card standard-options-card">
      <header class="section-header">
        <h3>${renderIconTitle(state.labels.standardOptionsSectionTitle, "slider.horizontal.3", undefined, "⚙️")}</h3>
      </header>
      <div class="controls">
        ${state.localizationOptions.length > 1
          ? `<label class="form-row">
                <span class="row-label">${escapeHTML(state.labels.languagePickerLabel)}</span>
                <span>
                  <select data-locale-picker aria-label="${escapeAttribute(state.labels.languagePickerLabel)}">
                    <option value="__system__" ${state.usingSystemDefaultLocale ? "selected" : ""}>${escapeHTML(systemLabel)}</option>
                    ${state.localizationOptions
              .map((option) => `<option value="${escapeAttribute(option.code)}" ${!state.usingSystemDefaultLocale && option.code === state.localizationCode ? "selected" : ""}>${escapeHTML(languageOptionLabel(option))}</option>`)
              .join("")}
                  </select>
                  <span class="field-note">${escapeHTML(state.usingSystemDefaultLocale ? systemLabel : currentName)}</span>
                </span>
              </label>`
          : ""}
        <label class="form-row">
          <span class="row-label">${escapeHTML(state.labels.iconSetPickerLabel)}</span>
          <select data-icon-set-picker aria-label="${escapeAttribute(state.labels.iconSetPickerLabel)}">
            <option value="emoji" ${state.iconSet === "emoji" ? "selected" : ""}>${escapeHTML(state.labels.iconSetEmojiLabel)}</option>
            <option value="platform" ${state.iconSet === "platform" ? "selected" : ""}>${escapeHTML(state.labels.iconSetBootstrapIconsLabel)}</option>
          </select>
        </label>
        <label class="form-row">
          <span class="row-label">${escapeHTML(state.labels.colorThemePickerLabel)}</span>
          <select data-color-theme-picker aria-label="${escapeAttribute(state.labels.colorThemePickerLabel)}">
            <option value="system" ${state.colorTheme === "system" ? "selected" : ""}>${escapeHTML(state.labels.colorThemeSystemLabel)}</option>
            <option value="light" ${state.colorTheme === "light" ? "selected" : ""}>${escapeHTML(state.labels.colorThemeLightLabel)}</option>
            <option value="dark" ${state.colorTheme === "dark" ? "selected" : ""}>${escapeHTML(state.labels.colorThemeDarkLabel)}</option>
          </select>
        </label>
        <label class="form-row">
          <span class="row-label">${escapeHTML(state.labels.webUIFontPickerLabel ?? "Web Font")}</span>
          <select data-web-font-picker aria-label="${escapeAttribute(state.labels.webUIFontPickerLabel ?? "Web Font")}">
            <option value="system" ${state.webUIFont !== "sfPro" ? "selected" : ""}>${escapeHTML(state.labels.webUIFontSystemLabel ?? "Current priority")}</option>
            <option value="sfPro" ${state.webUIFont === "sfPro" ? "selected" : ""}>${escapeHTML(state.labels.webUIFontSFProLabel ?? "SF Pro when available")}</option>
          </select>
        </label>
      </div>
    </section>
  `;
}
function languageOptionLabel(option: LocalizedOption) {
  return option.isAITranslated
    ? `${option.displayName} - ${state.labels.languageAITranslatedLabel ?? "AI translated"}`
    : option.displayName;
}
