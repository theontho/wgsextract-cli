import type { TerminalEntry } from "../../../shared/types.js";
import { escapeHTML } from "./dom.js";
import { nextFrame } from "./rerender.js";
import { state } from "./state.js";

// Scrollback kept per tab (in memory and in the DOM); old output is dropped in large steps.
const maxScrollbackChars = 1_000_000;
const trimmedScrollbackChars = 750_000;
const renderChunkChars = 32_768;
const truncatedNotice = "[earlier output trimmed]";
const pendingOutput = new Map<string, string>();
const renderedCharCounts = new WeakMap<Element, number>();
let flushScheduled = false;

/**
 * Renders terminal text as line-aligned block chunks plus a trailing partial line, so streamed output
 * can be appended and old output dropped by touching a few small nodes instead of one huge text node.
 */
export function renderTerminalText(entry: TerminalEntry | undefined): string {
    if (!entry) {
        return "";
    }
    const lineEnd = entry.body.lastIndexOf("\n") + 1;
    const chunks: string[] = [];
    for (let start = 0; start < lineEnd;) {
        const newline = entry.body.indexOf("\n", Math.min(start + renderChunkChars, lineEnd) - 1);
        const end = newline < 0 || newline >= lineEnd ? lineEnd : newline + 1;
        chunks.push(entry.body.slice(start, end));
        start = end;
    }
    return [
        entry.truncated ? `<span class="terminal-notice">${truncatedNotice}</span>` : "",
        ...chunks.map((chunk) => `<span class="terminal-chunk">${escapeHTML(chunk)}</span>`),
        `<span class="terminal-tail">${escapeHTML(entry.body.slice(lineEnd))}</span>`,
    ].join("");
}

/**
 * Appends streamed process output to a tab. The visible terminal is updated in place once per frame,
 * without re-rendering the app, so chatty commands neither stall the UI nor disturb form focus.
 */
export function appendTerminalOutput(entryID: string, text: string) {
    const entry = state.terminalEntries.find((candidate) => candidate.id === entryID);
    if (!entry || !text) {
        return;
    }
    entry.body += text;
    if (entry.body.length > maxScrollbackChars) {
        entry.body = entry.body.slice(-trimmedScrollbackChars);
        entry.truncated = true;
    }
    pendingOutput.set(entryID, (pendingOutput.get(entryID) ?? "") + text);
    if (!flushScheduled) {
        flushScheduled = true;
        nextFrame(flushTerminalOutput);
    }
}

/** A full render already shows every entry's current body. */
export function discardPendingTerminalOutput() {
    pendingOutput.clear();
}

function flushTerminalOutput() {
    flushScheduled = false;
    const log = globalThis.document?.querySelector<HTMLElement>(".terminal-log");
    const text = log?.dataset.terminalLogId ? pendingOutput.get(log.dataset.terminalLogId) : undefined;
    pendingOutput.clear();
    const pre = log?.querySelector("pre");
    const tail = pre?.querySelector(".terminal-tail");
    if (!log || !pre || !tail || !text) {
        return;
    }
    const pinnedToBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
    const combined = (tail.textContent ?? "") + text;
    const lineEnd = combined.lastIndexOf("\n") + 1;
    if (lineEnd > 0) {
        tail.before(terminalSpan("terminal-chunk", combined.slice(0, lineEnd)));
    }
    tail.textContent = combined.slice(lineEnd);
    trimRenderedScrollback(pre, text.length);
    if (pinnedToBottom) {
        log.scrollTop = log.scrollHeight;
    }
}

function trimRenderedScrollback(pre: HTMLElement, addedChars: number) {
    let count = renderedCharCounts.has(pre) ? (renderedCharCounts.get(pre) ?? 0) + addedChars : pre.textContent?.length ?? 0;
    if (count > maxScrollbackChars) {
        let notice = pre.querySelector(".terminal-notice");
        if (!notice) {
            notice = terminalSpan("terminal-notice", truncatedNotice);
            pre.prepend(notice);
        }
        let chunk = notice.nextElementSibling;
        while (count > trimmedScrollbackChars && chunk?.classList.contains("terminal-chunk")) {
            const next = chunk.nextElementSibling;
            count -= chunk.textContent?.length ?? 0;
            chunk.remove();
            chunk = next;
        }
    }
    renderedCharCounts.set(pre, count);
}

function terminalSpan(className: string, text: string) {
    const span = document.createElement("span");
    span.className = className;
    span.textContent = text;
    return span;
}
