import { allControls, disabledReason, displayCommand, isActionVisible, isPrecheckReady, missingPlaceholders } from "../../../../shared/rendering.js";
import { escapeAttribute, escapeHTML } from "../dom.js";
import { formatLabel, renderIcon, renderInlineError, renderLoadingInline } from "../model.js";
import { actionPrecheckKey, contextWithFileState, ensureActionPrecheck } from "../operations.js";
import { state } from "../state.js";
import type { ActionReference, ActionSpec, CommandContext, ManifestLocation, PrecheckResult } from "../../../../shared/types.js";

export function renderActions(actions: ActionSpec[], context: CommandContext, owner: ManifestLocation, compact = false) {
  const resolvedContext = contextWithFileState(context);
  return actions
    .filter((action) => isActionVisible(action, resolvedContext))
    .map((action) => {
      const reference: ActionReference = { ...owner, actionID: String(action.id ?? "") };
      const missing = missingPlaceholders(action.command, resolvedContext);
      const disabled = disabledReason(action, resolvedContext, state.labels.actionUnavailableTitle);
      const shouldRunPrecheck = isPrecheckReady(action.precheck, resolvedContext);
      const precheckKey = shouldRunPrecheck ? actionPrecheckKey(action, resolvedContext) : "";
      const precheck = shouldRunPrecheck ? ensureActionPrecheck(precheckKey, reference, resolvedContext) : null;
      const isLoadingPrecheck = precheckKey ? state.loadingActionPrechecks.has(precheckKey) : false;
      const precheckWarning = precheck?.severity === "warning" ? precheck.message : undefined;
      const disabledText = missing.length
        ? formatLabel(state.labels.actionMissingInputsFormat, { inputs: missing.map(actionPlaceholderLabel).join(", ") })
        : disabled ?? precheckWarning;
      const command = displayCommand(action.command, resolvedContext);
      const tooltipText = actionTooltipText(action.tooltip ?? command, disabledText ?? (isLoadingPrecheck ? state.labels.refreshingTitle : ""));
      const roleClass = action.role === "destructive" ? "danger" : action.role === "secondary" ? "secondary" : "primary";
      return `
        <span class="action-stack ${compact ? "compact" : ""}" data-tooltip="${escapeAttribute(tooltipText)}" ${disabledText || isLoadingPrecheck ? 'tabindex="0"' : ""}>
          ${precheck ? renderPrecheckBanner(precheck) : isLoadingPrecheck ? renderLoadingInline(state.labels.refreshingTitle) : ""}
          ${state.actionPrecheckErrors.has(precheckKey)
            ? renderInlineError(state.actionPrecheckErrors.get(precheckKey), `<button type="button" data-retry-precheck="${escapeAttribute(precheckKey)}">${escapeHTML(state.labels.retryButtonTitle)}</button>`)
            : ""}
          <button type="button" class="action-button ${roleClass} ${compact ? "compact" : ""} ${action.iconOnly ? "icon-only" : ""}" data-action-id="${escapeAttribute(action.id)}"
            data-action="${escapeAttribute(JSON.stringify(action))}"
            data-action-ref="${escapeAttribute(JSON.stringify(reference))}"
            data-action-context="${escapeAttribute(JSON.stringify(resolvedContext))}"
            ${disabledText || isLoadingPrecheck ? "disabled" : ""}>
            <span class="action-icon" aria-hidden="true">${renderIcon(action.iconName, action.textIcon, "▶")}</span>
            ${action.iconOnly ? "" : `<span>${escapeHTML(action.title)}</span>`}
          </button>
          ${actionEstimate(action)}
        </span>
      `;
    })
    .join("");
}

function actionEstimate(action: ActionSpec) {
  const label = estimatedDurationLabel(action.estimatedDurationMinutes);
  const accessibleLabel = escapeAttribute(`Estimated time ${label}`);
  return label
    ? `<span class="action-estimate" title="${accessibleLabel}" aria-label="${accessibleLabel}"><span aria-hidden="true">${renderIcon("clock", "🕒", "🕒")}</span><span>${escapeHTML(label)}</span></span>`
    : "";
}

function estimatedDurationLabel(minutes: unknown) {
  if (typeof minutes !== "number" || !Number.isFinite(minutes) || minutes < 0) {
    return "";
  }
  const wholeMinutes = Math.round(minutes);
  return `${Math.floor(wholeMinutes / 60)}:${String(wholeMinutes % 60).padStart(2, "0")}`;
}

function actionTooltipText(baseTooltip: unknown, statusTooltip: unknown) {
  return [baseTooltip, statusTooltip].filter((text, index, values) => text && values.indexOf(text) === index).join("\n");
}

function actionPlaceholderLabel(placeholder: string) {
  const normalized = normalizedPlaceholderLabelKey(placeholder);
  for (const control of allControls(state.manifest ?? {})) {
    if (control.id === normalized) {
      return control.label ?? placeholder;
    }
    for (const setting of control.settings ?? []) {
      if (setting.id === normalized ||
        setting.key === normalized ||
        `${control.id}.${setting.id}` === normalized ||
        `${control.id}.${setting.key}` === normalized) {
        return setting.label ?? placeholder;
      }
    }
  }
  return placeholder;
}

function normalizedPlaceholderLabelKey(placeholder: string) {
  const key = String(placeholder ?? "").replace(/^(config|row)\./, "");
  const fileStateSeparator = key.lastIndexOf(".");
  if (fileStateSeparator > 0) {
    const suffix = key.slice(fileStateSeparator + 1);
    if (suffix === "fileSize" || suffix === "fileSizeGB") {
      return key.slice(0, fileStateSeparator);
    }
  }
  return key;
}

export function renderPrecheckBanner(precheck: PrecheckResult) {
  const icon = precheck.severity === "warning" ? "⚠️" : "💽";
  return `
    <span class="precheck-banner ${escapeAttribute(precheck.severity)}">
      <span aria-hidden="true">${icon}</span>
      <span><strong>${escapeHTML(precheck.title)}</strong><span>${escapeHTML(precheck.message)}</span></span>
    </span>
  `;
}
