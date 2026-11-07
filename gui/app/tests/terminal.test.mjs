import assert from "node:assert/strict";
import test from "node:test";

globalThis.localStorage = {
  getItem() {
    return null;
  },
  setItem() {},
};
globalThis.window = { innerHeight: 900 };

const { createInitialState, state } = await import("../dist/web/src/client/state.js");
const { renderTerminalPane, terminalTextDirection } = await import("../dist/web/src/client/terminal.js");

test("terminal copy feedback renders only after copying", () => {
  Object.assign(state, createInitialState(), {
    labels: {
      terminalCommandOutputLabel: "Command output",
      terminalCopyTextLabel: "Copy terminal text",
      terminalCopiedTextLabel: "Copied!",
      terminalMainTabTitle: "Main",
      terminalCloseTabLabelFormat: "Close %{title}",
    },
  });

  let html = renderTerminalPane();
  assert.doesNotMatch(html, /Copied!/);
  assert.match(html, /class="bi bi-copy"/);
  assert.doesNotMatch(html, /class="bi bi-clipboard"/);

  state.terminalCopyFeedback = true;
  html = renderTerminalPane();
  assert.match(html, /Copied!/);
  assert.match(html, /role="status"/);
});

test("terminal log direction follows manifest instead of UI locale", () => {
  Object.assign(state, createInitialState(), {
    manifest: { terminalTextDirection: "ltr" },
    labels: {
      layoutDirection: "rtl",
      terminalCommandOutputLabel: "Command output",
      terminalCopyTextLabel: "Copy terminal text",
      terminalCopiedTextLabel: "Copied!",
      terminalMainTabTitle: "Main",
      terminalCloseTabLabelFormat: "Close %{title}",
    },
  });

  assert.equal(terminalTextDirection(), "ltr");
  assert.match(renderTerminalPane(), /class="terminal-log"[^>]*dir="ltr"/);

  state.manifest = { terminalTextDirection: "rtl" };

  assert.equal(terminalTextDirection(), "rtl");
  assert.match(renderTerminalPane(), /class="terminal-log"[^>]*dir="rtl"/);
});

test("terminal text renders as line-aligned chunks that reproduce the output exactly", async () => {
  const { renderTerminalText } = await import("../dist/web/src/client/terminal-output.js");
  const body = `${"0123456789".repeat(5000)}\n`.repeat(3) + "partial line";

  const html = renderTerminalText({ id: "t", kind: "command", title: "Run", body, truncated: true });
  const spans = [...html.matchAll(/<span class="(terminal-[a-z]+)">([^<]*)<\/span>/g)];

  assert.equal(spans[0][1], "terminal-notice");
  assert.ok(spans.filter(([, kind]) => kind === "terminal-chunk").length > 1);
  assert.ok(spans.filter(([, kind]) => kind === "terminal-chunk").every(([, , text]) => text.endsWith("\n")));
  assert.deepEqual(spans.at(-1).slice(1), ["terminal-tail", "partial line"]);
  assert.equal(spans.slice(1).map(([, , text]) => text).join(""), body);
});

test("streamed terminal output caps scrollback and marks the tab truncated", async () => {
  const { appendTerminalOutput } = await import("../dist/web/src/client/terminal-output.js");
  Object.assign(state, createInitialState(), { labels: { terminalMainTabTitle: "Main" } });
  state.terminalEntries = [{ id: "run", kind: "command", title: "Run", body: "" }];

  for (let index = 0; index < 120; index += 1) {
    appendTerminalOutput("run", `${String(index).padStart(9, "0")}${"x".repeat(9_990)}\n`);
  }

  const [entry] = state.terminalEntries;
  assert.equal(entry.truncated, true);
  assert.ok(entry.body.length <= 1_000_000);
  assert.ok(entry.body.endsWith(`${"000000119"}${"x".repeat(9_990)}\n`));
});
