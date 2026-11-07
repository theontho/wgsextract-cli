import { escapeAttribute, escapeHTML } from "../dom.js";
import { renderIconTitle } from "../model.js";
import { state } from "../state.js";

export function renderWorkspaceLocationSection() {
  const location = state.workspaceLocation;
  if (!location) {
    return "";
  }
  const locked = location.environmentOverride ? "disabled" : "";
  const label = state.labels.workspaceLocationFolderLabel;
  return `
    <section class="card workspace-location-card">
      <header class="section-header">
        <h3>${renderIconTitle(state.labels.workspaceLocationTitle, "folder", undefined, "📁")}</h3>
      </header>
      <div class="controls">
        <label class="form-row">
          <span class="row-label">${escapeHTML(label)}</span>
          <span>
            <span class="input-button-row workspace-location-row">
              <input type="text" data-workspace-location-input aria-label="${escapeAttribute(label)}" placeholder="${escapeAttribute(location.defaultDirectory)}" value="${escapeAttribute(location.configuredDirectory ?? "")}" ${locked}>
              <button type="button" class="secondary-button" data-workspace-location-choose ${locked}>${escapeHTML(state.labels.chooseButtonTitle)}</button>
              <button type="button" class="secondary-button" data-workspace-location-reset ${locked || !location.configuredDirectory ? "disabled" : ""}>${escapeHTML(state.labels.workspaceLocationUseDefaultTitle)}</button>
            </span>
            <span class="field-note">${escapeHTML(location.environmentOverride ? state.labels.workspaceLocationEnvironmentNote : state.labels.workspaceLocationNote)}</span>
            ${location.restartRequired ? `<span class="field-note" data-workspace-location-restart>${escapeHTML(state.labels.workspaceLocationRestartNote)}</span>` : ""}
          </span>
        </label>
        <div class="form-row">
          <span class="row-label">${escapeHTML(state.labels.workspaceLocationCurrentLabel)}</span>
          <code data-workspace-location-current>${escapeHTML(location.workspaceRoot)}</code>
        </div>
      </div>
    </section>
  `;
}
