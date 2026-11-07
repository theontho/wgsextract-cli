import { escapeAttribute, escapeHTML } from "../dom.js";
import { formatLabel, renderIconTitle } from "../model.js";
import { state } from "../state.js";

export function renderWorkspaceLocationSection() {
    const location = state.workspaceLocation;
    if (!location) {
        return "";
    }
    const busy = state.setupRun?.status === "running" || state.workspaceMove != null;
    const locked = location.environmentOverride || busy ? "disabled" : "";
    const label = state.labels.workspaceLocationFolderLabel;
    const draft = state.workspaceLocationDraft ?? location.configuredDirectory ?? "";
    return `
    <section class="card workspace-location-card">
      <header class="section-header">
        <h3>${renderIconTitle(state.labels.workspaceLocationTitle, "folder", undefined, "📁")}</h3>
      </header>
      <div class="controls">
        <div class="form-row">
          <span class="row-label">${escapeHTML(state.labels.workspaceLocationCurrentLabel)}</span>
          <code data-workspace-location-current>${escapeHTML(location.workspaceRoot)}</code>
        </div>
        <label class="form-row">
          <span class="row-label">${escapeHTML(label)}</span>
          <span>
            <span class="input-button-row workspace-location-row">
              <input type="text" data-workspace-location-input aria-label="${escapeAttribute(label)}" placeholder="${escapeAttribute(location.defaultDirectory)}" value="${escapeAttribute(draft)}" ${locked}>
              <button type="button" class="secondary-button" data-workspace-location-choose ${locked}>${escapeHTML(state.labels.chooseButtonTitle)}</button>
              <button type="button" class="secondary-button" data-workspace-location-move ${locked || !draft ? "disabled" : ""}>${escapeHTML(state.labels.workspaceLocationMoveTitle)}</button>
              <button type="button" class="secondary-button" data-workspace-location-reset ${locked || location.usingDefault ? "disabled" : ""}>${escapeHTML(state.labels.workspaceLocationUseDefaultTitle)}</button>
            </span>
            <span class="field-note">${escapeHTML(location.environmentOverride ? state.labels.workspaceLocationEnvironmentNote : state.labels.workspaceLocationNote)}</span>
          </span>
        </label>
        ${renderWorkspaceMoveStatus()}
      </div>
    </section>
  `;
}

function renderWorkspaceMoveStatus() {
    const move = state.workspaceMove;
    if (!move || !state.workspaceLocation) {
        return "";
    }
    if (move.running) {
        return `<p class="field-note" data-workspace-location-moving>${escapeHTML(formatLabel(state.labels.workspaceLocationMovingFormat, { percent: move.percent }))}</p>`;
    }
    const destination = move.directory ?? state.workspaceLocation.defaultDirectory;
    return `
        <div class="workspace-location-confirm">
          <p class="field-note">${escapeHTML(formatLabel(state.labels.workspaceLocationMoveConfirm, { path: destination }))}</p>
          <span class="input-button-row">
            <button type="button" class="action-button primary" data-workspace-location-confirm>${escapeHTML(state.labels.workspaceLocationMoveTitle)}</button>
            <button type="button" class="secondary-button" data-workspace-location-cancel>${escapeHTML(state.labels.workspaceLocationCancelTitle)}</button>
          </span>
        </div>
  `;
}
