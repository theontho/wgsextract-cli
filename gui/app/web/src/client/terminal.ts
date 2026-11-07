import { escapeAttribute, escapeHTML } from "./dom.js";
import type { TerminalEntry, TerminalStatus } from "../../../shared/types.js";
import { formatLabel } from "./model.js";
import { state } from "./state.js";
import { renderTerminalText } from "./terminal-output.js";
export { appendTerminalOutput, discardPendingTerminalOutput } from "./terminal-output.js";
/** Cancels the run streaming into a terminal tab (actions and setup) when the tab is closed. */
export const runningActionControllers = new Map<string, AbortController>();

export function renderTerminal() {
    const entry = terminalTabs()[state.activeTerminalIndex] ?? terminalTabs()[0];
    return `<pre>${renderTerminalText(entry)}</pre>`;
}
export function renderTerminalPane() {
    const tabs = terminalTabs();
    const entry = tabs[state.activeTerminalIndex] ?? tabs[0];
    const copyLabel = state.labels.terminalCopyTextLabel ?? "Copy terminal text";
    const textDirection = terminalTextDirection();
    return `
    <section class="terminal-panel" aria-label="${escapeHTML(state.labels.terminalCommandOutputLabel)}">
      <header class="terminal-header">
        <span class="terminal-glyph" aria-hidden="true">⌘</span>
        <div class="terminal-tabs">
          ${tabs
        .map((tab, index) => `
              <span class="terminal-tab-wrap ${index === state.activeTerminalIndex ? "active" : ""} ${escapeAttribute(tab.kind)}">
                <button type="button" class="terminal-tab" data-terminal-tab="${index}" ${tab.status ? `data-tooltip="${escapeAttribute(terminalStatusTooltip(tab.status))}"` : ""}>
                  ${tab.kind === "command" ? `<span class="mini-spinner" aria-hidden="true"></span>` : terminalStatusGlyph(tab.kind, tab.status)}
                  <span>${escapeHTML(tab.title)}</span>
                </button>
                ${tab.kind === "main"
        ? ""
        : `<button type="button" class="terminal-tab-close" data-terminal-tab-close="${index}" aria-label="${escapeAttribute(formatLabel(state.labels.terminalCloseTabLabelFormat, { title: tab.title }))}">×</button>`}
              </span>`)
        .join("")}
        </div>
        <button type="button" class="terminal-copy" data-terminal-copy title="${escapeAttribute(copyLabel)}" aria-label="${escapeAttribute(copyLabel)}">
          <i class="bi bi-copy" aria-hidden="true"></i>
        </button>
        ${state.terminalCopyFeedback ? `<span class="terminal-copy-feedback" role="status">${escapeHTML(state.labels.terminalCopiedTextLabel ?? "Copied!")}</span>` : ""}
      </header>
      <div class="terminal-log" data-terminal-log-id="${escapeAttribute(entry?.id ?? "")}" dir="${escapeAttribute(textDirection)}">${renderTerminal()}</div>
    </section>
  `;
}
export function terminalTextDirection() {
    return state.manifest?.terminalTextDirection === "rtl" ? "rtl" : "ltr";
}
export function appendTerminal(kind: string, title: unknown, body = "", command = ""): string {
    ensureMainTerminal();
    if (kind === "config") {
        const main = state.terminalEntries[0];
        if (!main) {
            throw new Error("Main terminal was not initialized.");
        }
        main.body = [main.body, title, body].filter(Boolean).join("\n");
        state.activeTerminalIndex = 0;
        return "main";
    }
    const id = crypto.randomUUID();
    state.terminalEntries.push({ id, kind, title: String(title ?? ""), body, command });
    state.terminalEntries = state.terminalEntries.slice(-40);
    state.activeTerminalIndex = Math.max(0, state.terminalEntries.length - 1);
    return id;
}
export function resetTerminalEntries() {
    for (const controller of runningActionControllers.values()) {
        controller.abort();
    }
    runningActionControllers.clear();
    state.terminalEntries = [];
    state.activeTerminalIndex = 0;
    ensureMainTerminal();
}
export function terminalTabs(): TerminalEntry[] {
    ensureMainTerminal();
    return state.terminalEntries;
}
export function ensureMainTerminal() {
    if (state.terminalEntries[0]?.kind === "main") {
        state.terminalEntries[0].id ??= "main";
        state.terminalEntries[0].title = state.labels.terminalMainTabTitle ?? "Main";
        return;
    }
    const hadEntries = state.terminalEntries.length > 0;
    state.terminalEntries.unshift({
        id: "main",
        kind: "main",
        title: state.labels.terminalMainTabTitle ?? "Main",
        body: "",
        command: "main",
    });
    state.activeTerminalIndex = hadEntries ? state.activeTerminalIndex + 1 : 0;
}
export function closeTerminalTab(index: number) {
    if (index <= 0 || index >= state.terminalEntries.length) {
        return;
    }
    const tab = state.terminalEntries[index];
    if (!tab) {
        return;
    }
    runningActionControllers.get(tab.id)?.abort();
    runningActionControllers.delete(tab.id);
    state.terminalEntries.splice(index, 1);
    if (state.activeTerminalIndex === index) {
        state.activeTerminalIndex = Math.max(0, index - 1);
    }
    else if (state.activeTerminalIndex > index) {
        state.activeTerminalIndex -= 1;
    }
    state.activeTerminalIndex = Math.min(state.activeTerminalIndex, state.terminalEntries.length - 1);
}
export function terminalToggleTitle(): string {
    return state.isTerminalVisible
        ? state.labels.terminalHideOutputLabel ?? "Hide Command Output"
        : state.labels.terminalShowOutputLabel ?? "Show Command Output";
}
export function terminalStatusGlyph(kind: string, status?: TerminalStatus): string {
    switch (kind) {
        case "success":
            return '<span class="terminal-status success" aria-hidden="true">●</span>';
        case "warning":
            return `<span class="terminal-status warning" aria-hidden="true">${escapeHTML(status?.symbol ?? "▲")}</span>`;
        case "error":
            return `<span class="terminal-status error" aria-hidden="true">${escapeHTML(status?.symbol ?? "●")}</span>`;
        case "config":
            return '<span class="terminal-status config" aria-hidden="true">●</span>';
        default:
            return "";
    }
}
export function terminalExitStatus(exitCode: number | null, command: string): TerminalStatus {
    const reference = state.exitCodeReference.get(Number(exitCode));
    const severity = reference?.severity === "warning" ? "warning" : "error";
    const blurb = reference?.summary ?? state.labels.terminalNonzeroExitSummary;
    return {
        severity,
        symbol: severity === "warning" ? "▲" : "✕",
        title: reference?.title ?? formatLabel(state.labels.terminalExitCodeTitleFormat, { code: exitCode }),
        ...(blurb != null ? { blurb } : {}),
        detail: formatLabel(state.labels.terminalExitDetailFormat, { command, code: exitCode }),
    };
}
export function terminalProcessErrorStatus(command: string, message: string): TerminalStatus {
    return {
        severity: "error",
        symbol: "✕",
        ...(state.labels.terminalProcessErrorTitle != null ? { title: state.labels.terminalProcessErrorTitle } : {}),
        ...(state.labels.terminalProcessErrorSummary != null ? { blurb: state.labels.terminalProcessErrorSummary } : {}),
        detail: `${command}\n${message}`,
    };
}
export function terminalStatusTooltip(status: TerminalStatus): string {
    return `${status.title}\n${status.blurb}\n\n${status.detail}`;
}
