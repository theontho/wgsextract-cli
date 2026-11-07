import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer, request as httpRequest } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const { createRequestHandler } = await import("../dist/web/src/server/routes.js");
const { streamNDJSON } = await import("../dist/web/src/server/ndjson.js");
const { takeApiToken } = await import("../dist/web/src/server/request-guard.js");

const apiToken = "test-token-0123456789abcdefghijklmnopqrstuvwxyz";

function testManifest() {
  return {
    id: "test-bundle",
    displayName: "Test Bundle",
    pages: [
      {
        id: "main",
        title: "Main",
        sections: [
          {
            id: "run",
            actions: [{ id: "run", title: "Run", command: { executable: "tool", arguments: ["{{sample}}"] } }],
            controls: [
              {
                id: "library",
                kind: "libraryList",
                label: "Library",
                dataSource: { path: "scripts/list.sh", arguments: ["{{sample}}"] },
                rowActions: [{ id: "delete", title: "Delete", command: { executable: "static-delete", arguments: ["{{row.id}}"] } }],
              },
              {
                id: "settings",
                kind: "configEditor",
                label: "Settings",
                configFile: { path: "{{bundleWorkspace}}/config.toml" },
                settings: [{ id: "threads", key: "threads", label: "Threads", value: "4" }],
              },
            ],
          },
        ],
      },
    ],
    setup: { steps: [{ id: "node", kind: "pathTool", label: "Find Node", value: "node" }] },
  };
}

async function withTestServer(t, overrides = {}) {
  const tempRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-routes-"));
  t.after(async () => {
    await rm(tempRoot, { force: true, recursive: true });
  });
  await writeFile(path.join(tempRoot, "index.html"), '<!doctype html><head><meta name="gfc-api-token" content=""></head>\n');

  const calls = { loadBundle: [], localizedLoads: [], runProcess: [], shutdown: [] };
  const manifest = testManifest();
  const context = {
    addDevReloadClient() {},
    apiToken,
    appVersion: "1.2.3",
    bundleRoot: tempRoot,
    defaultLocale: "en",
    distRoot: tempRoot,
    dynamicActions: new Map(),
    enableDevReload: false,
    localizedBundleLoader: {
      async load(locale, preferredLocales) {
        calls.localizedLoads.push({ locale, preferredLocales });
        return { manifest, labels: {} };
      },
    },
    async loadBundle(requestedSource) {
      calls.loadBundle.push(requestedSource);
      return { bundleRootPath: path.join(tempRoot, "workspace"), sourceRootPath: requestedSource };
    },
    manifest,
    maxBodyBytes: 1_048_576,
    repoRoot: path.resolve(".."),
    async runProcess(executable, args, options = {}) {
      calls.runProcess.push({ executable, args, options });
      await options.onStdout?.("hello from process\n");
      return { exitCode: 0, stdout: "ok\n", stderr: "" };
    },
    shutdown(reason) {
      calls.shutdown.push(reason);
    },
    sourceBundleRoot: tempRoot,
    webRoot: tempRoot,
    ...overrides,
  };

  const server = createServer(createRequestHandler(context));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return { baseURL: `http://127.0.0.1:${port}`, port, calls, context, tempRoot };
}

function apiFetch(baseURL, route, { body, headers = {}, method } = {}) {
  return fetch(`${baseURL}${route}`, {
    method: method ?? (body === undefined ? "GET" : "POST"),
    headers: {
      "x-gfc-token": apiToken,
      ...(body === undefined ? {} : { "content-type": "application/json" }),
      ...headers,
    },
    ...(body === undefined ? {} : { body: typeof body === "string" ? body : JSON.stringify(body) }),
  });
}

/** fetch() cannot spoof Host, so attacks that rely on it go through http.request. */
function rawRequest(port, route, { method = "POST", headers = {}, body = "" } = {}) {
  return new Promise((resolve, reject) => {
    const request = httpRequest({ host: "127.0.0.1", port, path: route, method, headers }, (response) => {
      let text = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        text += chunk;
      });
      response.on("end", () => resolve({ status: response.statusCode, headers: response.headers, text }));
    });
    request.on("error", reject);
    request.end(body);
  });
}

async function readNDJSON(response) {
  const text = await response.text();
  return text.trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

const runContext = { fieldValues: { sample: "HG001" }, checkedOptions: {}, configValues: {}, rowValues: {} };

test("audit regression: cross-origin simple requests and DNS rebinding cannot run commands", async (t) => {
  const { port, calls } = await withTestServer(t);
  const attack = JSON.stringify({
    action: { title: "pwn", command: { executable: "/bin/sh", arguments: ["-c", "touch /tmp/pwned"] } },
    context: runContext,
  });
  const attempts = [
    // A text/plain "simple" CORS request from an evil page: no preflight, no token.
    { "content-type": "text/plain", host: `127.0.0.1:${port}`, origin: "http://evil.example" },
    // DNS rebinding: same-origin from the browser's view, but the Host names the attacker's domain.
    { "content-type": "application/json", host: `evil.example:${port}`, origin: `http://evil.example:${port}` },
    // Spoofed Origin/Host that look local, still without the per-launch token.
    { "content-type": "application/json", host: `127.0.0.1:${port}`, origin: `http://127.0.0.1:${port}` },
  ];
  for (const headers of attempts) {
    for (const route of ["/api/run", "/api/run/stream", "/api/config/save", "/api/bundle/load", "/api/setup/stream"]) {
      const response = await rawRequest(port, route, { headers, body: attack });
      assert.ok([401, 403].includes(response.status), `${route} with ${JSON.stringify(headers)} returned ${response.status}`);
    }
  }
  assert.deepEqual(calls.runProcess, []);
});

test("API requests need the token, a local Host, a matching Origin, and JSON bodies", async (t) => {
  const { port, calls } = await withTestServer(t);
  const host = `127.0.0.1:${port}`;
  const body = JSON.stringify({ action: { pageID: "main", sectionID: "run", actionID: "run" }, context: runContext });
  const json = "application/json";

  const statuses = {
    missingToken: (await rawRequest(port, "/api/manifest", { method: "GET", headers: { host } })).status,
    wrongToken: (await rawRequest(port, "/api/manifest", { method: "GET", headers: { host, "x-gfc-token": `${apiToken.slice(0, -1)}X` } })).status,
    foreignHost: (await rawRequest(port, "/api/manifest", { method: "GET", headers: { host: `attacker.test:${port}`, "x-gfc-token": apiToken } })).status,
    wrongPortHost: (await rawRequest(port, "/api/manifest", { method: "GET", headers: { host: "127.0.0.1:1", "x-gfc-token": apiToken } })).status,
    foreignOrigin: (await rawRequest(port, "/api/run/stream", { headers: { host, origin: "http://evil.example", "x-gfc-token": apiToken, "content-type": json }, body })).status,
    textPlain: (await rawRequest(port, "/api/run/stream", { headers: { host, "x-gfc-token": apiToken, "content-type": "text/plain" }, body })).status,
    foreignHostIndex: (await rawRequest(port, "/", { method: "GET", headers: { host: `attacker.test:${port}` } })).status,
    localhostAlias: (await rawRequest(port, "/api/manifest", { method: "GET", headers: { host: `localhost:${port}`, "x-gfc-token": apiToken } })).status,
    sameOrigin: (await rawRequest(port, "/api/run/stream", { headers: { host, origin: `http://${host}`, "x-gfc-token": apiToken, "content-type": "application/json; charset=utf-8" }, body })).status,
  };

  assert.deepEqual(statuses, {
    missingToken: 401,
    wrongToken: 401,
    foreignHost: 403,
    wrongPortHost: 403,
    foreignOrigin: 403,
    textPlain: 415,
    foreignHostIndex: 403,
    localhostAlias: 200,
    sameOrigin: 200,
  });
  assert.equal(calls.runProcess.length, 1);
});

test("index.html bootstraps the token for the page and every response carries a strict CSP", async (t) => {
  const { baseURL } = await withTestServer(t);

  const response = await fetch(`${baseURL}/`);
  const html = await response.text();

  assert.equal(response.status, 200);
  assert.match(html, new RegExp(`<meta name="gfc-api-token" content="${apiToken}">`));
  assert.equal(response.headers.get("cache-control"), "no-store");
  const csp = response.headers.get("content-security-policy");
  assert.match(csp, /default-src 'none'/);
  assert.match(csp, /script-src 'self'(;|$)/);
  assert.match(csp, /frame-ancestors 'none'/);
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.equal(response.headers.get("cross-origin-resource-policy"), "same-origin");
});

test("index.html leaves the token out when the launcher injects it", async (t) => {
  const { baseURL } = await withTestServer(t, { apiTokenInjectedByLauncher: true });

  const html = await (await fetch(`${baseURL}/`)).text();

  assert.match(html, /<meta name="gfc-api-token" content="">/);
  assert.doesNotMatch(html, new RegExp(apiToken));
});

test("launcher tokens are taken out of the environment and validated", () => {
  const environment = { GFC_API_TOKEN: apiToken, PATH: "/bin" };
  assert.equal(takeApiToken(environment), apiToken);
  assert.deepEqual(environment, { PATH: "/bin" });
  assert.match(takeApiToken({}), /^[A-Za-z0-9_-]{43}$/);
  assert.throws(() => takeApiToken({ GFC_API_TOKEN: "short" }), /at least 32/);
});

test("GET /api/manifest localizes with app version and preferred locales", async (t) => {
  const { baseURL, calls } = await withTestServer(t);

  const response = await apiFetch(baseURL, "/api/manifest?locale=fr", { headers: { "accept-language": "es;q=0.5,de;q=0.9,*;q=1" } });
  const body = await response.json();

  assert.equal(response.status, 200);
  assert.equal(body.manifest.id, "test-bundle");
  assert.equal(body.appVersion, "1.2.3");
  assert.deepEqual(calls.localizedLoads, [{ locale: "fr", preferredLocales: ["de", "es"] }]);
});

test("GET /api/manifest omits a blank app version", async (t) => {
  const { baseURL } = await withTestServer(t, { appVersion: " " });

  const body = await (await apiFetch(baseURL, "/api/manifest")).json();

  assert.equal(Object.hasOwn(body, "appVersion"), false);
});

test("POST /api/run/stream renders the referenced manifest action server-side", async (t) => {
  const { baseURL, calls } = await withTestServer(t);

  const response = await apiFetch(baseURL, "/api/run/stream", {
    body: {
      action: { pageID: "main", sectionID: "run", actionID: "run", command: { executable: "/bin/sh", arguments: ["-c", "evil"] } },
      context: { ...runContext, bundleRootPath: "/elsewhere", fileStateValues: { "sample.exists": "true" } },
    },
  });
  const events = await readNDJSON(response);

  assert.equal(response.status, 200);
  assert.deepEqual(events.map((event) => event.type), ["start", "output", "complete"]);
  assert.equal(events[1].stream, "stdout");
  assert.equal(events[2].result.exitCode, 0);
  assert.match(events[2].result.command, /tool HG001$/);
  assert.deepEqual(calls.runProcess.map((call) => [call.executable, call.args]), [["tool", ["HG001"]]]);
});

test("POST /api/run/stream rejects command bodies and unknown actions without running anything", async (t) => {
  const { baseURL, calls } = await withTestServer(t);

  const legacy = await apiFetch(baseURL, "/api/run/stream", {
    body: { action: { title: "Run", command: { executable: "tool", arguments: [] } }, context: runContext },
  });
  const unknown = await apiFetch(baseURL, "/api/run/stream", {
    body: { action: { pageID: "main", sectionID: "run", actionID: "nope" }, context: runContext },
  });
  const badContext = await apiFetch(baseURL, "/api/run/stream", {
    body: { action: { pageID: "main", sectionID: "run", actionID: "run" }, context: { fieldValues: { sample: { nested: true } } } },
  });
  const removedEndpoint = await apiFetch(baseURL, "/api/run", {
    body: { action: { pageID: "main", sectionID: "run", actionID: "run" }, context: runContext },
  });

  assert.equal(legacy.status, 400);
  assert.match((await legacy.json()).error, /Missing required field: pageID/);
  assert.equal(unknown.status, 404);
  assert.equal(badContext.status, 400);
  assert.equal(removedEndpoint.status, 404);
  assert.deepEqual(calls.runProcess, []);
});

test("data source row actions replace the declared ones for that control", async (t) => {
  const processCalls = [];
  const { baseURL } = await withTestServer(t, {
    async runProcess(executable, args) {
      processCalls.push({ executable, args });
      if (executable.endsWith("list.sh")) {
        return {
          exitCode: 0,
          stderr: "",
          stdout: JSON.stringify({ rows: [{ id: "hg38" }], rowActions: [{ id: "delete", title: "Delete", command: { executable: "dynamic-delete", arguments: ["{{row.id}}"] } }] }),
        };
      }
      return { exitCode: 0, stdout: "", stderr: "" };
    },
  });
  const reference = { pageID: "main", sectionID: "run", controlID: "library", actionID: "delete" };
  const rowContext = { ...runContext, rowValues: { id: "hg38" } };

  await readNDJSON(await apiFetch(baseURL, "/api/run/stream", { body: { action: reference, context: rowContext } }));
  const payload = await (await apiFetch(baseURL, "/api/datasource", { body: { source: { pageID: "main", sectionID: "run", controlID: "library" }, context: runContext } })).json();
  await readNDJSON(await apiFetch(baseURL, "/api/run/stream", { body: { action: reference, context: rowContext } }));
  const missingSource = await apiFetch(baseURL, "/api/datasource", { body: { source: { pageID: "main", sectionID: "run" }, context: runContext } });

  assert.deepEqual(payload.rows, [{ id: "hg38" }]);
  assert.deepEqual(processCalls.map((call) => [path.basename(call.executable), call.args]), [
    ["static-delete", ["hg38"]],
    ["list.sh", ["HG001"]],
    ["dynamic-delete", ["hg38"]],
  ]);
  assert.equal(missingSource.status, 404);
});

test("POST /api/precheck resolves the precheck from the manifest", async (t) => {
  const { baseURL } = await withTestServer(t);

  const setup = await apiFetch(baseURL, "/api/precheck", { body: { target: { kind: "setup" }, labels: {} } });
  const action = await apiFetch(baseURL, "/api/precheck", { body: { target: { kind: "action", pageID: "main", sectionID: "run", actionID: "run" }, context: runContext, labels: {} } });
  const raw = await apiFetch(baseURL, "/api/precheck", { body: { precheck: { diskSpaceGB: "1" }, labels: {} } });

  assert.equal(setup.status, 200);
  assert.equal(await setup.json(), null);
  assert.equal(action.status, 200);
  assert.equal(raw.status, 400);
});

test("POST /api/setup/stream emits setup completion", async (t) => {
  const { baseURL, calls } = await withTestServer(t);

  const response = await apiFetch(baseURL, "/api/setup/stream", { body: { locale: "en" } });
  const events = await readNDJSON(response);

  assert.equal(response.status, 200);
  assert.equal(events.at(-1).type, "complete");
  assert.equal(events.at(-1).result.status, "ok");
  assert.equal(calls.runProcess.length, 1);
});

test("disconnecting a setup stream cancels the running step and frees the workspace", async (t) => {
  const runs = [];
  const { baseURL } = await withTestServer(t, {
    runProcess(_executable, _args, options = {}) {
      return new Promise((resolve, reject) => {
        runs.push(options.signal);
        options.signal?.addEventListener("abort", () => reject(new Error("Process cancelled.")), { once: true });
      });
    },
  });
  const waitForRuns = async (count) => {
    while (runs.length < count) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
  };

  const first = await apiFetch(baseURL, "/api/setup/stream", { body: {} });
  await waitForRuns(1);
  const concurrent = await readNDJSON(await apiFetch(baseURL, "/api/setup/stream", { body: {} }));
  await first.body.cancel();
  while (!runs[0].aborted) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  // Once the cancelled run has settled the workspace lock is free again.
  const retry = await apiFetch(baseURL, "/api/setup/stream", { body: {} });
  await waitForRuns(2);
  await retry.body.cancel();

  assert.equal(concurrent.at(-1).type, "complete");
  assert.match(concurrent.at(-1).result.error, /already running/);
  assert.equal(runs.length, 2);
});

test("bundles load only from folders picked in the native dialog", async (t) => {
  const picker = createServer((request, response) => {
    assert.equal(request.headers["x-gfc-token"], apiToken);
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ path: "/picked/bundle", kind: "directory", cancelled: false }));
  });
  await new Promise((resolve) => picker.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => picker.close(resolve)));
  const { baseURL, calls } = await withTestServer(t, { nativePicker: { port: picker.address().port, token: apiToken } });

  const unpicked = await apiFetch(baseURL, "/api/bundle/load", { body: { path: "/tmp/source-bundle" } });
  const pick = await (await apiFetch(baseURL, "/api/path/pick", { body: { kind: "directory", title: "Bundle" } })).json();
  const picked = await apiFetch(baseURL, "/api/bundle/load", { body: { path: pick.path } });
  const reused = await apiFetch(baseURL, "/api/bundle/load", { body: { path: pick.path } });

  assert.equal(unpicked.status, 403);
  assert.equal(picked.status, 200);
  assert.equal((await picked.json()).sourceRootPath, "/picked/bundle");
  assert.equal(reused.status, 403);
  assert.deepEqual(calls.loadBundle, ["/picked/bundle"]);
});

test("settings are only written to the declared file type of a declared settings editor", async (t) => {
  const { baseURL, tempRoot } = await withTestServer(t);
  const save = (body) => apiFetch(baseURL, "/api/config/save", { body });

  const declared = await save({ controlID: "settings", values: { threads: "8" } });
  const other = await save({ controlID: "settings", path: path.join(tempRoot, "other.toml"), values: { threads: "2", injected: "x" } });
  const shellProfile = await save({ controlID: "settings", path: path.join(tempRoot, ".zshrc"), values: { threads: "1" } });
  const unknownControl = await save({ controlID: "library", values: {} });
  const badState = await apiFetch(baseURL, "/api/state/save", { body: { state: { configFilePaths: { settings: path.join(tempRoot, "evil.sh") } } } });

  assert.equal(declared.status, 200);
  assert.equal(await readFile(path.join(tempRoot, "config.toml"), "utf8"), 'threads = "8"\n');
  assert.equal(other.status, 200);
  assert.equal(await readFile(path.join(tempRoot, "other.toml"), "utf8"), 'threads = "2"\n');
  assert.equal(shellProfile.status, 400);
  assert.equal(unknownControl.status, 404);
  assert.equal(badState.status, 400);
});

test("bundle files are served from the workspace only", async (t) => {
  const { baseURL, tempRoot } = await withTestServer(t);
  await writeFile(path.join(tempRoot, "icon.png"), "png");

  const icon = await fetch(`${baseURL}/bundle-file?path=icon.png`);
  const escape = await fetch(`${baseURL}/bundle-file?path=..%2F..%2Fetc%2Fpasswd`);

  assert.equal(icon.status, 200);
  assert.equal(icon.headers.get("content-type"), "image/png");
  assert.equal(escape.status, 404);
});

test("NDJSON streams abort the run and drop the connection when the client goes away", async () => {
  const response = new FailingStreamResponse(2);
  let runSignal;

  await streamNDJSON(response, async (emit, signal) => {
    runSignal = signal;
    await emit({ type: "start" });
    await emit({ type: "output", text: "chunk\n" });
    await new Promise((resolve) => setImmediate(resolve));
    await emit({ type: "output", text: "after failure\n" });
    await emit({ type: "complete" });
  }, (error) => ({ type: "error", error }));

  assert.equal(runSignal.aborted, true);
  assert.equal(response.destroyed, true);
  assert.match(response.destroyError.message, /client disconnected/);
  assert.doesNotMatch(response.body, /after failure/);
});

class FailingStreamResponse extends EventEmitter {
  headersSent = false;
  writableEnded = false;
  destroyed = false;
  destroyError = undefined;
  body = "";
  #writeCount = 0;
  #failOnWrite;

  constructor(failOnWrite) {
    super();
    this.#failOnWrite = failOnWrite;
  }

  writeHead() {
    this.headersSent = true;
  }

  write(chunk) {
    this.#writeCount += 1;
    this.body += String(chunk);
    if (this.#writeCount === this.#failOnWrite) {
      setImmediate(() => this.emit("error", new Error("client disconnected")));
      return false;
    }
    return true;
  }

  end() {
    this.writableEnded = true;
  }

  destroy(error) {
    this.destroyed = true;
    this.destroyError = error;
    this.emit("close");
  }
}
