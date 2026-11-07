import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.window = { innerHeight: 900 };

const { runBundleTest } = await import("../dist/web/src/server/bundle-test-runner.js");
const { parseResultDocument, resultNotice } = await import("../dist/shared/result-document.js");
const { createInitialState, state } = await import("../dist/web/src/client/state.js");
const { renderResultNotice, renderResultSummary } = await import("../dist/web/src/client/result-view.js");

// A CLI following the wgsextract.result/1 contract: logs on stderr and, with --json, one document
// on stdout. MODE picks the run: ok writes report.txt (5 bytes); wrong-size records 99 bytes;
// usage refuses its options with exit 2.
const fakeCli = String.raw`
import { writeFileSync } from "node:fs";
import path from "node:path";
const mode = process.argv[2];
console.error("log: running " + mode);
if (!process.argv.includes("--json")) {
  console.log("a human summary");
  process.exit(0);
}
const report = path.resolve("report.txt");
const documents = {
  ok: { status: "ok", exit_code: 0, outputs: [{ path: report, role: "report", kind: "text", size_bytes: 5 }] },
  "wrong-size": { status: "ok", exit_code: 0, outputs: [{ path: report, role: "report", kind: "text", size_bytes: 99 }] },
  usage: { status: "failed", exit_code: 2, outputs: [], error: { kind: "usage", message: "--a and --b cannot be combined." } },
};
if (mode !== "usage") writeFileSync(report, "hello");
console.log(JSON.stringify({ schema: "wgsextract.result/1", command: "fake", warnings: [], metrics: { reads: 10 }, ...documents[mode] }, null, 2));
process.exit(documents[mode].exit_code);
`;

async function withResultBundle(t) {
  const root = await mkdtemp(path.join(tmpdir(), "gfc-result-document-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const bundleRoot = path.join(root, "Bundle");
  await mkdir(path.join(bundleRoot, "scripts"), { recursive: true });
  await writeFile(path.join(bundleRoot, "scripts", "cli.mjs"), fakeCli, "utf8");
  await writeFile(path.join(bundleRoot, "manifest.json"), JSON.stringify({
    id: "dev.guiforcli.result-document-fixture",
    displayName: "Result Document Fixture",
    summary: "Fixture bundle.",
    resultDocument: { format: "wgsextract.result/1", executables: [process.execPath], arguments: ["--json"] },
    pages: [{
      id: "main",
      title: "Main",
      summary: "Main page.",
      sections: [{
        id: "inputs",
        controls: [{ id: "mode", label: "Mode", kind: "text" }],
        actions: [{ id: "run", title: "Run", command: { executable: process.execPath, arguments: ["{{bundleRoot}}/scripts/cli.mjs", "{{mode}}"] } }],
      }],
    }],
  }), "utf8");
  return { bundleRoot, workspaceRoot: path.join(root, "Workspace") };
}

function step(mode, extra) {
  return { kind: "action", id: mode, actionID: "run", inputs: { fieldValues: { mode } }, continueOnFailure: true, ...extra };
}

test("bundle tests check the result document instead of the printed output", async (t) => {
  const { bundleRoot, workspaceRoot } = await withResultBundle(t);

  const report = await runBundleTest(bundleRoot, {
    steps: [
      step("ok", { expectedResult: { status: "ok", outputs: ["report"] } }),
      step("ok", { expectedResult: { outputs: ["coverage"] } }),
      step("wrong-size", { expectedResult: { status: "ok", outputs: ["report"] } }),
      step("usage", { expectedExitCodes: [2], expectedResult: { status: "failed", errorKind: "usage" } }),
      step("usage", { expectedExitCodes: [2], expectedResult: { errorKind: "missing_dependency" } }),
    ],
  }, { workspaceURL: workspaceRoot });

  assert.deepEqual(report.steps.map((entry) => entry.status), ["passed", "failed", "failed", "passed", "failed"]);
  const [ok, missingRole, wrongSize, usage, wrongKind] = report.steps;
  // The command was asked for the document, which is kept in the report and not shown as output.
  assert.match(ok.command, /cli\.mjs ok --json$/);
  assert.equal(ok.result.metrics.reads, 10);
  assert.match(ok.output, /log: running ok/);
  assert.doesNotMatch(ok.output, /wgsextract\.result/);
  assert.match(missingRole.error, /no output with role coverage/);
  assert.match(wrongSize.error, /5 bytes on disk but the result recorded 99/);
  assert.equal(usage.exitCode, 2);
  assert.match(wrongKind.error, /Expected result error kind missing_dependency but got usage/);
});

test("a dry run shows the command with the result document arguments", async (t) => {
  const { bundleRoot, workspaceRoot } = await withResultBundle(t);

  const report = await runBundleTest(bundleRoot, { steps: [step("ok")] }, { workspaceURL: workspaceRoot, dryRun: true });

  assert.match(report.steps[0].command, /cli\.mjs ok --json$/);
});

test("failed runs map to a usage or missing-tool notice, from the document or the exit code", () => {
  const document = (error) => parseResultDocument({ schema: "wgsextract.result/1", status: "failed", error }, "wgsextract.result/1");

  assert.deepEqual(resultNotice(document({ kind: "usage", message: "Choose --ref." }), 2, "ignored\n"), { kind: "usage", message: "Choose --ref." });
  assert.deepEqual(
    resultNotice(document({ kind: "missing_dependency", message: "Tool not found: bcftools. Run: pixi install -e default" }), 3, ""),
    { kind: "missing_dependency", message: "Tool not found: bcftools. Run: pixi install -e default" },
  );
  assert.equal(resultNotice(document({ kind: "failure", message: "Input file not found" }), 1, ""), undefined);
  // Argument parsing and wrapper scripts fail before a document is written.
  assert.deepEqual(resultNotice(null, 2, "usage: wgsextract info\nwgsextract info: error: --input is required\n"), {
    kind: "usage",
    message: "wgsextract info: error: --input is required",
  });
  assert.deepEqual(resultNotice(null, 127, "Error: Pixi was not found. Run the WGS Extract setup action again.\n"), {
    kind: "missing_dependency",
    message: "Error: Pixi was not found. Run the WGS Extract setup action again.",
  });
  assert.equal(resultNotice(null, 1, "boom\n"), undefined);
  assert.equal(parseResultDocument({ schema: "other/1", status: "ok" }, "wgsextract.result/1"), null);
});

test("the result summary lists outputs to open, warnings and metrics; the notice offers setup", () => {
  Object.assign(state, createInitialState(), { labels: {} });
  const document = parseResultDocument({
    schema: "wgsextract.result/1",
    status: "ok",
    manifest: "/out/sample.manifest.json",
    metrics: { mean_depth: 0.123456789, sex: "<unknown>" },
    warnings: [{ code: "low_coverage", message: "Coverage is below 1x." }],
    outputs: [
      { path: "/out/sample.cram", role: "alignment", kind: "cram", size_bytes: 2048 },
      { path: "/out/gone.bai", role: "alignment_index", exists: false },
    ],
  }, "wgsextract.result/1");

  const summary = renderResultSummary(document);

  assert.match(summary, /low_coverage<\/code> Coverage is below 1x\./);
  assert.match(summary, /<dt>mean_depth<\/dt><dd>0\.123457<\/dd>/);
  assert.match(summary, /&lt;unknown&gt;/);
  assert.match(summary, /2\.0 KB/);
  assert.match(summary, /data-result-open="\/out\/sample\.cram"/);
  assert.match(summary, /data-result-reveal="\/out\/sample\.manifest\.json"/);
  // A listed output that no longer exists can't be opened.
  assert.doesNotMatch(summary, /data-result-open="\/out\/gone\.bai"/);

  assert.match(renderResultNotice({ kind: "missing_dependency", message: "Run: pixi install -e gpu" }), /data-result-open-setup[\s\S]*Open Setup/);
  assert.doesNotMatch(renderResultNotice({ kind: "usage", message: "Choose --ref." }), /data-result-open-setup/);
});
