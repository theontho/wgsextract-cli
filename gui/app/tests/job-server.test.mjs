import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const { createProcessManager } = await import("../dist/web/src/server/process-runner.js");
const { createJobServer } = await import("../dist/web/src/server/job-server.js");
const { runActionPreferringServer } = await import("../dist/web/src/server/job-server-action.js");

// A job server speaking the protocol in docs/job-server.md. `slow` runs until cancelled;
// `quick` finishes at once and logs the parameters it received; anything else is refused.
const fakeServer = String.raw`
import { createServer } from "node:http";
const token = "secret-" + process.pid;
const jobs = new Map();
const watchers = new Map();
function send(job, type, data) {
  job.events.push({ type, data });
  for (const response of watchers.get(job.id) ?? []) {
    response.write("event: " + type + "\ndata: " + JSON.stringify(data) + "\n\n");
    if (type === "status" && ["succeeded", "failed", "cancelled"].includes(data.status)) response.end();
  }
}
function finish(job, status, exitCode) {
  Object.assign(job, { status, exit_code: exitCode });
  send(job, "status", { status });
}
const server = createServer(async (request, response) => {
  if (request.headers.authorization !== "Bearer " + token) {
    response.writeHead(401).end();
    return;
  }
  let body = "";
  for await (const chunk of request) body += chunk;
  const [, kind, ...rest] = new URL(request.url, "http://x").pathname.replace(/^\/v1/, "").split("/");
  if (request.method === "POST" && kind === "ops") {
    const operation = rest.join("/");
    if (!["slow", "quick"].includes(operation)) {
      response.writeHead(400, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: { code: "path_not_allowed", message: "not allowed" } }));
      return;
    }
    const job = { id: "job" + (jobs.size + 1), status: "running", events: [], log: "params " + body + "\n" };
    jobs.set(job.id, job);
    response.writeHead(202, { "content-type": "application/json" }).end(JSON.stringify(job));
    send(job, "status", { status: "running" });
    send(job, "progress", { stage: "reading", message: "chr1", fraction: 0.5 });
    if (operation === "quick") setTimeout(() => finish(job, "succeeded", 0), 50);
    return;
  }
  const job = jobs.get(rest[0]);
  if (kind !== "jobs" || !job) {
    response.writeHead(404).end();
  } else if (request.method === "DELETE") {
    response.writeHead(202).end();
    setTimeout(() => finish(job, "cancelled", null), 50);
  } else if (rest[1] === "events") {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.write(": keepalive\n\n");
    watchers.set(job.id, [...(watchers.get(job.id) ?? []), response]);
    for (const event of job.events) response.write("event: " + event.type + "\ndata: " + JSON.stringify(event.data) + "\n\n");
    if (job.status !== "running") response.end();
  } else if (rest[1] === "log") {
    response.writeHead(200).end(job.log);
  } else {
    const { events, ...record } = job;
    response.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify(record));
  }
});
server.listen(0, "127.0.0.1", () => {
  console.log("starting up");
  console.log(JSON.stringify({ url: "http://127.0.0.1:" + server.address().port + "/v1/", token }));
});
`;

async function withBundle(t) {
  const bundleRoot = await mkdtemp(path.join(tmpdir(), "gui-for-cli-job-server-"));
  t.after(() => rm(bundleRoot, { recursive: true, force: true }));
  await writeFile(path.join(bundleRoot, "server.mjs"), fakeServer);
  const manager = createProcessManager({ maxOutputBytes: 1_048_576, maxErrorBytes: 65_536 });
  t.after(() => manager.terminateAllProcesses());
  const startServer = (args = ["{{bundleRoot}}/server.mjs"]) =>
    createJobServer({ command: { executable: process.execPath, arguments: args }, startupTimeoutSeconds: 10 }, bundleRoot, manager.runProcess);
  return { bundleRoot, manager, startServer };
}

function action(operation, parameters = {}) {
  return {
    id: "coverage",
    title: "Coverage",
    command: { executable: process.execPath, arguments: ["-e", "console.log('ran the command')"] },
    serverOperation: { operation, parameters },
  };
}

async function run(bundle, spec, jobServer, signal) {
  const events = [];
  const context = { fieldValues: { bam: "/data/sample.bam", ref: "" } };
  const result = await runActionPreferringServer(spec, context, signal, bundle.bundleRoot, bundle.manager.runProcess, jobServer, (event) => events.push(event));
  return { result, events, output: events.filter((event) => event.type === "output").map((event) => event.text).join("") };
}

test("an action with a serverOperation runs as a job, with its progress in the output", async (t) => {
  const bundle = await withBundle(t);
  const { result, events, output } = await run(bundle, action("quick", { input: "{{bam}}", ref: "{{ref}}", bins: true }), bundle.startServer());

  assert.equal(result.exitCode, 0);
  assert.equal(events[0].type, "start");
  assert.equal(events.at(-1).type, "complete");
  assert.match(output, /\[progress\] reading: chr1 \(50%\)/);
  assert.match(output, /\[job\] succeeded/);
  // An empty placeholder leaves its parameter out; non-string values are sent as written.
  assert.match(output, /params \{"input":"\/data\/sample\.bam","bins":true\}/);
  assert.doesNotMatch(output, /ran the command/);
});

test("cancelling a server action cancels its job", async (t) => {
  const bundle = await withBundle(t);
  const jobServer = bundle.startServer();
  const controller = new AbortController();
  const running = run(bundle, action("slow"), jobServer, controller.signal);
  setTimeout(() => controller.abort(), 300);

  await assert.rejects(running, /Process cancelled\./);
  const address = await jobServer.address();
  const job = await (await fetch(`${address.url}/jobs/job1`, { headers: { authorization: `Bearer ${address.token}` } })).json();
  assert.equal(job.status, "cancelled");
});

test("an action runs its command when the job server refuses the job", async (t) => {
  const bundle = await withBundle(t);
  const { result, output } = await run(bundle, action("refused"), bundle.startServer());

  assert.equal(result.exitCode, 0);
  assert.match(output, /ran the command/);
});

test("an action runs its command when the job server can't start", async (t) => {
  const bundle = await withBundle(t);
  const { result, output } = await run(bundle, action("quick"), bundle.startServer(["-e", "process.exit(3)"]));

  assert.equal(result.exitCode, 0);
  assert.match(output, /ran the command/);
});

test("a job server stopped with the other processes starts again on next use", async (t) => {
  const bundle = await withBundle(t);
  const jobServer = bundle.startServer();
  const first = await jobServer.address();
  await bundle.manager.terminateAllProcesses();

  const { result, output } = await run(bundle, action("quick"), jobServer);
  assert.equal(result.exitCode, 0);
  assert.match(output, /\[job\] succeeded/);
  assert.notEqual((await jobServer.address()).url, first.url);
});
