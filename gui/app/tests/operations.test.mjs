import assert from "node:assert/strict";
import test from "node:test";

globalThis.localStorage = {
  getItem() {
    return null;
  },
  setItem() {},
};
globalThis.window = { innerHeight: 900 };
globalThis.document = {
  querySelector() {
    return null;
  },
};

const { createInitialState, state } = await import("../dist/web/src/client/state.js");
const { ensureDataSource, openBundleWorkspace, retryDataSource, runAction, runSetup } = await import("../dist/web/src/client/operations.js");
const { renderActions, renderConfigSetting, renderDropdownControl } = await import("../dist/web/src/client/view.js");

function resetState() {
  Object.assign(state, createInitialState(), {
    manifest: {
      setup: {
        steps: [
          { id: "install", kind: "setupScript", label: "Install tool", value: "scripts/install.sh" },
          { id: "check", kind: "pixiRun", label: "Check deps", value: "wgsextract", arguments: ["deps", "check"] },
        ],
      },
    },
    labels: {
      setupTitle: "Setup",
      setupRunningTitle: "Running setup...",
      terminalMainTabTitle: "Main",
    },
    bundleRootPath: "/bundle",
  });
}

test("Settings toggles display native and form-encoded boolean values correctly", () => {
  resetState();
  const control = { id: "settings" };
  const setting = { id: "enabled", kind: "toggle", label: "Enabled", value: false };
  for (const [value, checked] of [[true, true], ["true", true], [false, false], ["false", false]]) {
    state.configValues = { "settings.enabled": value };
    const html = renderConfigSetting(control, setting, {});
    assert.equal(/type="checkbox"\s+checked/.test(html), checked);
  }
});

test("Settings numeric dropdowns select the loaded number without converting stored values", () => {
  resetState();
  const control = { id: "settings" };
  const setting = {
    id: "workers", kind: "dropdown", label: "Workers", value: 4,
    options: [{ id: "4", title: "Four" }, { id: "8", title: "Eight" }],
  };
  state.configValues = { "settings.workers": 8 };
  const html = renderConfigSetting(control, setting, {});
  assert.match(html, /value="8"\s+selected/);
  assert.doesNotMatch(html, /value="4"\s+selected/);
  assert.equal(state.configValues["settings.workers"], 8);
  state.fieldValues.workers = 8;
  const boundDropdown = renderDropdownControl({ id: "workers", label: "Workers", options: setting.options });
  assert.match(boundDropdown, /value="8"\s+selected/);
  assert.equal(state.fieldValues.workers, 8);
});

async function waitUntil(predicate) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (predicate()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error("Timed out waiting for condition.");
}

function setupStreamResponse() {
  const encoder = new TextEncoder();
  let controller;
  const body = new ReadableStream({
    start(streamController) {
      controller = streamController;
    },
  });
  return {
    response: new Response(body, {
      status: 200,
      headers: { "content-type": "application/x-ndjson" },
    }),
    write(event) {
      controller.enqueue(encoder.encode(`${JSON.stringify(event)}\n`));
    },
    writeRaw(text) {
      controller.enqueue(encoder.encode(text));
    },
    close() {
      controller.close();
    },
  };
}

test("streams setup output into a selectable setup terminal tab", async () => {
  resetState();
  const originalFetch = globalThis.fetch;
  const requests = [];
  const stream = setupStreamResponse();
  globalThis.fetch = (path, options) => {
    requests.push({ path, options });
    if (path === "/api/state/save") {
      return Promise.resolve(new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
    }
    return Promise.resolve(stream.response);
  };

  try {
    const setupPromise = runSetup();
    await waitUntil(() => requests.length === 1);

    assert.equal(requests[0].path, "/api/setup/stream");
    assert.equal(requests[0].options.method, "POST");
    assert.equal(requests[0].options.headers["content-type"], "application/json");
    assert.equal(JSON.parse(requests[0].options.body).locale, "");
    assert.equal(state.terminalEntries.length, 2);
    assert.equal(state.terminalEntries[0].kind, "main");
    assert.equal(state.terminalEntries[0].body, "");
    assert.equal(state.terminalEntries[1].title, "Setup");
    assert.equal(state.activeTerminalID, state.terminalEntries[1].id);
    assert.match(state.terminalEntries[1].body, /Running setup/);
    assert.doesNotMatch(state.terminalEntries[0].body, /Install tool/);

    stream.write({
      type: "step-start",
      step: {
        id: "install",
        label: "Install tool",
        kind: "setupScript",
        command: "/bin/sh /bundle/scripts/install.sh",
      },
    });
    await waitUntil(() => /==> Install tool/.test(state.terminalEntries[1].body));

    assert.match(state.terminalEntries[1].body, /==> Install tool/);
    assert.match(state.terminalEntries[1].body, /\$ \/bin\/sh \/bundle\/scripts\/install\.sh/);

    stream.write({ type: "output", id: "install", stream: "stdout", text: "installed\n" });
    await waitUntil(() => /installed/.test(state.terminalEntries[1].body));

    stream.write({
      type: "step-complete",
      result: {
        id: "install",
        label: "Install tool",
        kind: "setupScript",
        command: "/bin/sh /bundle/scripts/install.sh",
        exitCode: 0,
        stdout: "installed",
        stderr: "",
        status: "ok",
      },
    });
    await waitUntil(() => /\[ok\] Install tool/.test(state.terminalEntries[1].body));

    assert.match(state.terminalEntries[1].body, /installed/);
    assert.match(state.terminalEntries[1].body, /\[ok\] Install tool/);

    stream.write({
      type: "step-start",
      step: {
        id: "check",
        label: "Check deps",
        kind: "pixiRun",
        command: "/usr/bin/env pixi run wgsextract deps check",
      },
    });
    await waitUntil(() => /==> Check deps/.test(state.terminalEntries[1].body));

    assert.match(state.terminalEntries[1].body, /==> Check deps/);

    stream.write({ type: "output", id: "check", stream: "stdout", text: "deps ok\n" });
    stream.write({
      type: "step-complete",
      result: {
        id: "check",
        label: "Check deps",
        kind: "pixiRun",
        command: "/usr/bin/env pixi run wgsextract deps check",
        exitCode: 0,
        stdout: "deps ok",
        stderr: "",
        status: "ok",
      },
    });
    stream.write({
      type: "complete",
      result: {
        status: "ok",
        results: [
          { id: "install", label: "Install tool", status: "ok", exitCode: 0 },
          { id: "check", label: "Check deps", status: "ok", exitCode: 0 },
        ],
      },
    });
    stream.close();
    await setupPromise;

    assert.equal(state.setupRun.status, "ok");
    const saveRequest = requests.find((request) => request.path === "/api/state/save");
    assert.ok(saveRequest);
    assert.equal(JSON.parse(saveRequest.options.body).state.setupRun.status, "ok");
    assert.equal(state.terminalEntries[1].kind, "success");
    assert.match(state.terminalEntries[1].body, /deps ok/);
    assert.match(state.terminalEntries[1].body, /\[ok\] Check deps/);
    assert.doesNotMatch(state.terminalEntries[0].body, /deps ok/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("setup warning completion is not marked as an error tab", async () => {
  resetState();
  const originalFetch = globalThis.fetch;
  const requests = [];
  const stream = setupStreamResponse();
  globalThis.fetch = (path, options) => {
    requests.push({ path, options });
    if (path === "/api/state/save") {
      return Promise.resolve(new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
    }
    return Promise.resolve(stream.response);
  };

  try {
    const setupPromise = runSetup();
    await waitUntil(() => requests.length === 1);

    stream.write({
      type: "complete",
      result: {
        status: "warning",
        results: [
          { id: "install", label: "Install tool", status: "ok", exitCode: 0 },
          { id: "check", label: "Check deps", status: "warning", exitCode: 1 },
        ],
      },
    });
    stream.close();
    await setupPromise;

    assert.equal(state.setupRun.status, "warning");
    assert.equal(state.terminalEntries[1].kind, "success");
    const saveRequest = requests.find((request) => request.path === "/api/state/save");
    assert.ok(saveRequest);
    assert.equal(JSON.parse(saveRequest.options.body).state.setupRun.status, "warning");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("setup stream EOF before complete marks setup failed", async () => {
  resetState();
  const originalFetch = globalThis.fetch;
  const requests = [];
  const stream = setupStreamResponse();
  globalThis.fetch = (path, options) => {
    requests.push({ path, options });
    if (path === "/api/state/save") {
      return Promise.resolve(new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
    }
    return Promise.resolve(stream.response);
  };

  try {
    const setupPromise = runSetup();
    await waitUntil(() => requests.length === 1);

    stream.write({ type: "step-start", step: { id: "install", label: "Install tool", command: "/bin/sh install.sh" } });
    stream.close();
    await setupPromise;

    assert.equal(state.setupRun.status, "failed");
    assert.match(state.setupRun.error, /Setup stream ended before completion/);
    assert.equal(state.terminalEntries[1].kind, "error");
    const saveRequest = requests.find((request) => request.path === "/api/state/save");
    assert.ok(saveRequest);
    assert.equal(JSON.parse(saveRequest.options.body).state.setupRun.status, "failed");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("data source errors do not block retries", async () => {
  resetState();
  const originalFetch = globalThis.fetch;
  let attempts = 0;
  globalThis.fetch = (path) => {
    attempts += 1;
    if (attempts === 1) {
      return Promise.resolve(new Response(JSON.stringify({ error: "temporary failure" }), { status: 500, headers: { "content-type": "application/json" } }));
    }
    return Promise.resolve(new Response(JSON.stringify({ options: [{ id: "hg38", label: "GRCh38" }] }), { status: 200, headers: { "content-type": "application/json" } }));
  };

  try {
    ensureDataSource("control:reference", { command: { executable: "echo" } }, {});
    await waitUntil(() => state.dataSourceErrors.has("control:reference"));

    retryDataSource("control:reference");
    ensureDataSource("control:reference", { command: { executable: "echo" } }, {});
    await waitUntil(() => state.dataSourcePayloads.has("control:reference"));

    assert.equal(attempts, 2);
    assert.equal(state.dataSourceErrors.has("control:reference"), false);
    assert.equal(state.fieldValues.reference, "hg38");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("setup holds data sources while running and reloads failed ones when it ends", async () => {
  resetState();
  const originalFetch = globalThis.fetch;
  const dataSourceRequests = [];
  const stream = setupStreamResponse();
  globalThis.fetch = (path) => {
    if (path === "/api/setup/stream") {
      return Promise.resolve(stream.response);
    }
    if (path === "/api/state/save") {
      return Promise.resolve(new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
    }
    dataSourceRequests.push(path);
    if (dataSourceRequests.length === 1) {
      return Promise.resolve(new Response(JSON.stringify({ error: "runtime is not installed" }), { status: 500, headers: { "content-type": "application/json" } }));
    }
    return Promise.resolve(new Response(JSON.stringify({ options: [{ id: "hg38", label: "GRCh38" }] }), { status: 200, headers: { "content-type": "application/json" } }));
  };

  try {
    ensureDataSource("control:reference", { command: { executable: "echo" } }, {});
    await waitUntil(() => state.dataSourceErrors.has("control:reference"));

    const setupPromise = runSetup();
    await waitUntil(() => state.setupRun?.status === "running");
    retryDataSource("control:reference");
    ensureDataSource("control:reference", { command: { executable: "echo" } }, {});
    assert.equal(dataSourceRequests.length, 1);

    state.dataSourceErrors.set("control:reference", "failed before setup");
    stream.write({ type: "complete", result: { status: "ok", results: [] } });
    stream.close();
    await setupPromise;
    assert.equal(state.dataSourceErrors.has("control:reference"), false);

    ensureDataSource("control:reference", { command: { executable: "echo" } }, {});
    await waitUntil(() => state.dataSourcePayloads.has("control:reference"));
    assert.equal(dataSourceRequests.length, 2);
    assert.equal(state.fieldValues.reference, "hg38");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("opens the bundle workspace through the server API", async () => {
  resetState();
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = (path, options) => {
    requests.push({ path, options });
    return Promise.resolve(new Response("{\"ok\":true}", { status: 200, headers: { "content-type": "application/json" } }));
  };

  try {
    await openBundleWorkspace();

    assert.equal(requests.length, 1);
    assert.equal(requests[0].path, "/api/open-bundle-workspace");
    assert.equal(requests[0].options.method, "POST");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

const actionReference = { pageID: "main", sectionID: "run", actionID: "run" };

test("action terminal streams progress and logs only command GUI inputs", async () => {
  resetState();
  state.manifest.pages = [
    {
      sections: [
        {
          controls: [
            { id: "genome_library", label: "Genome library", kind: "path" },
            { id: "api_token", label: "API token", kind: "text" },
            {
              id: "settings",
              label: "Settings",
              kind: "configEditor",
              settings: [{ id: "output_directory", key: "output_directory", label: "Output directory" }],
            },
          ],
        },
      ],
    },
  ];
  const originalFetch = globalThis.fetch;
  const requests = [];
  const stream = setupStreamResponse();
  globalThis.fetch = (path, options) => {
    requests.push({ path, options });
    return Promise.resolve(stream.response);
  };

  try {
    const actionPromise = runAction(
      { title: "Download Test Genome", command: { executable: "/bin/echo", arguments: ["{{genome_library}}", "{{api_token}}"] } },
      {
        fieldValues: { genome_library: "/tmp/genomes", api_token: "super-secret" },
        checkedOptions: {},
        configValues: { "settings.output_directory": "/tmp/out", genome_library: "/tmp/genomes" },
        rowValues: {},
        bundleRootPath: "/bundle",
        placeholderLabels: {
          genome_library: "Genome library",
          api_token: "API token",
          "settings.output_directory": "Output directory",
        },
      }, actionReference);
    await waitUntil(() => requests.length === 1);

    assert.equal(requests[0].path, "/api/run/stream");
    assert.equal(requests[0].options.method, "POST");
    assert.match(state.terminalEntries[1].body, /with inputs Genome library=\/tmp\/genomes, API token=<redacted>/);
    assert.doesNotMatch(state.terminalEntries[1].body, /super-secret/);
    assert.doesNotMatch(state.terminalEntries[1].body, /Output directory/);
    assert.match(state.terminalEntries[1].body, /\[queued\] Preparing command environment/);

    stream.write({ type: "start", command: "/bin/echo /tmp/genomes" });
    await waitUntil(() => /\[running\] Started/.test(state.terminalEntries[1].body));

    stream.write({ type: "output", stream: "stdout", text: "downloading\n" });
    await waitUntil(() => /downloading/.test(state.terminalEntries[1].body));

    stream.write({
      type: "complete",
      result: {
        exitCode: 0,
        stdout: "downloading\n",
        stderr: "",
        command: "/bin/echo /tmp/genomes",
      },
    });
    stream.close();
    await actionPromise;

    assert.match(
      state.terminalEntries[1].body,
      /Executing action "Download Test Genome" with inputs Genome library=\/tmp\/genomes, API token=<redacted>/);
    assert.doesNotMatch(state.terminalEntries[1].body, /with args/);
    assert.doesNotMatch(state.terminalEntries[1].body, /super-secret/);
    assert.match(state.terminalEntries[1].body, /exit 0/);
    assert.equal(state.terminalEntries[1].kind, "success");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("an action's result document reaches its terminal tab, and a usage error its notice until the next run", async () => {
  resetState();
  const originalFetch = globalThis.fetch;
  const streams = [setupStreamResponse(), setupStreamResponse()];
  let requests = 0;
  globalThis.fetch = () => Promise.resolve(streams[requests++].response);
  const context = { fieldValues: {}, checkedOptions: {}, configValues: {}, rowValues: {}, bundleRootPath: "/bundle", placeholderLabels: {} };
  const action = { title: "Call SVs", command: { executable: "/bin/echo", arguments: [] } };
  const document = { format: "wgsextract.result/1", status: "failed", error: { kind: "usage", message: "--pacbio and --ont cannot be combined." }, outputs: [], warnings: [], metrics: {} };

  try {
    const failing = runAction(action, context, actionReference);
    await waitUntil(() => requests === 1);
    streams[0].write({ type: "complete", result: { exitCode: 2, command: "/bin/echo", stderr: "", resultDocument: document } });
    streams[0].close();
    await failing;

    assert.equal(state.terminalEntries.at(-1).result.status, "failed");
    assert.deepEqual([...state.actionResultNotices.values()], [{ kind: "usage", message: "--pacbio and --ont cannot be combined." }]);

    const rerun = runAction(action, context, actionReference);
    await waitUntil(() => requests === 2);
    assert.equal(state.actionResultNotices.size, 0);
    streams[1].close();
    await rerun;
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("review regression: an older run finishing last does not restore its notice, and a row's notice stays on its row", async () => {
  resetState();
  const originalFetch = globalThis.fetch;
  const streams = [setupStreamResponse(), setupStreamResponse(), setupStreamResponse()];
  let requests = 0;
  globalThis.fetch = () => Promise.resolve(streams[requests++].response);
  const rowContext = (genome) => ({ fieldValues: {}, checkedOptions: {}, configValues: {}, rowValues: { genome }, bundleRootPath: "/bundle", placeholderLabels: {} });
  const action = { id: "run", title: "Index", command: { executable: "/bin/echo", arguments: [] } };
  const usage = { exitCode: 2, command: "/bin/echo", stderr: "", resultDocument: { format: "wgsextract.result/1", status: "failed", error: { kind: "usage", message: "Choose a FASTA." }, outputs: [], warnings: [], metrics: {} } };
  const finish = (index, result) => {
    streams[index].write({ type: "complete", result });
    streams[index].close();
  };

  try {
    const older = runAction(action, rowContext("hg38"), actionReference);
    await waitUntil(() => requests === 1);
    const newer = runAction(action, rowContext("hg38"), actionReference);
    await waitUntil(() => requests === 2);
    finish(1, { exitCode: 0, command: "/bin/echo", stderr: "" });
    await newer;
    finish(0, usage);
    await older;
    assert.equal(state.actionResultNotices.size, 0);

    const failing = runAction(action, rowContext("hg19"), actionReference);
    await waitUntil(() => requests === 3);
    finish(2, usage);
    await failing;
    const owner = { pageID: "main", sectionID: "run" };
    assert.match(renderActions([action], rowContext("hg19"), owner, true), /result-notice/);
    assert.doesNotMatch(renderActions([action], rowContext("hg38"), owner, true), /result-notice/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("action terminal marks truncated streams as errors", async () => {
  resetState();
  const originalFetch = globalThis.fetch;
  const stream = setupStreamResponse();
  globalThis.fetch = () => Promise.resolve(stream.response);

  try {
    const actionPromise = runAction(
      { title: "Long Action", command: { executable: "/bin/echo", arguments: [] } },
      {
        fieldValues: {},
        checkedOptions: {},
        configValues: {},
        rowValues: {},
        bundleRootPath: "/bundle",
        placeholderLabels: {},
      }, actionReference);

    stream.write({ type: "start", command: "/bin/echo" });
    await waitUntil(() => /\[running\] Started/.test(state.terminalEntries[1].body));
    stream.close();
    await actionPromise;

    assert.equal(state.terminalEntries[1].kind, "error");
    assert.match(state.terminalEntries[1].body, /Action stream ended before completion/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("action terminal marks streamed error events as errors", async () => {
  resetState();
  const originalFetch = globalThis.fetch;
  const stream = setupStreamResponse();
  globalThis.fetch = () => Promise.resolve(stream.response);

  try {
    const actionPromise = runAction(
      { title: "Long Action", command: { executable: "/bin/echo", arguments: [] } },
      {
        fieldValues: {},
        checkedOptions: {},
        configValues: {},
        rowValues: {},
        bundleRootPath: "/bundle",
        placeholderLabels: {},
      }, actionReference);

    stream.write({ type: "start", command: "/bin/echo" });
    stream.write({ type: "output", stream: "stdout", text: "before failure\n" });
    stream.write({ type: "error", error: "Simulated stream failure" });
    stream.close();
    await actionPromise;

    assert.equal(state.terminalEntries[1].kind, "error");
    assert.match(state.terminalEntries[1].body, /before failure/);
    assert.match(state.terminalEntries[1].body, /Simulated stream failure/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("action terminal reports invalid stream JSON events clearly", async () => {
  resetState();
  const originalFetch = globalThis.fetch;
  const stream = setupStreamResponse();
  globalThis.fetch = () => Promise.resolve(stream.response);

  try {
    const actionPromise = runAction(
      { title: "Long Action", command: { executable: "/bin/echo", arguments: [] } },
      {
        fieldValues: {},
        checkedOptions: {},
        configValues: {},
        rowValues: {},
        bundleRootPath: "/bundle",
        placeholderLabels: {},
      }, actionReference);

    stream.write({ type: "start", command: "/bin/echo" });
    stream.writeRaw("{not-json}\n");
    stream.close();
    await actionPromise;

    assert.equal(state.terminalEntries[1].kind, "error");
    assert.match(state.terminalEntries[1].body, /Action stream returned invalid JSON event: \{not-json\}/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
