import assert from "node:assert/strict";
import { mkdtemp, rm, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const { loadBundleTestPlan, runBundleTest } = await import("../dist/web/src/server/bundle-test-runner.js");

test("web bundle test runner streams setup and action progress", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "gfc-web-bundle-test-"));
  const bundleRoot = path.join(root, "Bundle");
  const workspaceRoot = path.join(root, "Workspace");
  await mkdir(path.join(bundleRoot, "scripts"), { recursive: true });
  await writeFile(
    path.join(bundleRoot, "scripts", "action.mjs"),
    "console.log(`action:${process.argv[2]}`);\nconsole.error('stderr:ok');\n",
    "utf8",
  );
  await writeFile(
    path.join(bundleRoot, "manifest.json"),
    JSON.stringify({
      id: "dev.guiforcli.web-bundle-test-fixture",
      displayName: "Web Bundle Test Fixture",
      summary: "Fixture bundle.",
      setup: {
        steps: [
          { id: "node", kind: "pathTool", label: "Find Node", value: process.execPath },
        ],
      },
      pages: [
        {
          id: "main",
          title: "Main",
          summary: "Main page.",
          sections: [
            {
              id: "inputs",
              controls: [{ id: "sample", label: "Sample", kind: "text" }],
              actions: [
                {
                  id: "say-hello",
                  title: "Say hello",
                  command: {
                    executable: process.execPath,
                    arguments: ["{{bundleRoot}}/scripts/action.mjs", "{{sample}}"],
                  },
                },
              ],
            },
          ],
        },
      ],
    }),
    "utf8",
  );
  const events = [];

  try {
    const report = await runBundleTest(
      bundleRoot,
      {
        name: "web smoke",
        inputs: { fieldValues: { sample: "Ada" } },
        steps: [
          { kind: "setup" },
          { kind: "action", actionID: "say-hello", requiredOutput: ["action:Ada"] },
        ],
      },
      {
        workspaceURL: workspaceRoot,
        progressHandler: (event) => events.push(event),
      },
    );

    assert.equal(report.status, "passed");
    assert.equal(report.summary.total, 2);
    assert.equal(report.summary.passed, 2);
    assert.match(report.steps[0].output, /Find Node/);
    assert.equal(report.steps[1].output, "action:Ada\nstderr:ok\n");
    assert(events.some((event) => event.type === "message" && /Bundle test started: web smoke/.test(event.text)));
    assert(events.some((event) => event.type === "message" && /Step 1\/2 started: setup/.test(event.text)));
    assert(events.some((event) => event.type === "message" && /Step 2\/2 passed/.test(event.text)));
    assert(events.some((event) => event.type === "command-output" && /action:Ada/.test(event.text)));
    assert(events.some((event) => event.type === "command-output" && /stderr:ok/.test(event.text)));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("web bundle test runner reports missing required inputs", async () => {
  await withWebBundleTestFixture("console.log(`action:${process.argv[2]}`);\n", async ({ bundleRoot, workspaceRoot }) => {
    const events = [];

    const report = await runBundleTest(
      bundleRoot,
      { steps: [{ kind: "action", actionID: "say-hello" }] },
      { workspaceURL: workspaceRoot, progressHandler: (event) => events.push(event) },
    );

    assert.equal(report.status, "failed");
    assert.equal(report.summary.failed, 1);
    assert.match(report.steps[0].error, /Missing input values: sample/);
    assert(events.some((event) => event.type === "message" && /Missing input values: sample/.test(event.text)));
  });
});

test("web bundle test runner reports command failures", async () => {
  await withWebBundleTestFixture(
    "console.log('before failure');\nconsole.error('stderr failure');\nprocess.exit(7);\n",
    async ({ bundleRoot, workspaceRoot }) => {
      const events = [];

      const report = await runBundleTest(
        bundleRoot,
        {
          inputs: { fieldValues: { sample: "Ada" } },
          steps: [{ kind: "action", actionID: "say-hello" }],
        },
        { workspaceURL: workspaceRoot, progressHandler: (event) => events.push(event) },
      );

      assert.equal(report.status, "failed");
      assert.equal(report.summary.failed, 1);
      assert.equal(report.steps[0].exitCode, 7);
      assert.match(report.steps[0].output, /before failure/);
      assert.match(report.steps[0].output, /stderr failure/);
      assert(events.some((event) => event.type === "command-output" && /before failure/.test(event.text)));
      assert(events.some((event) => event.type === "command-output" && /stderr failure/.test(event.text)));
    },
  );
});

test("web bundle test runner reports invalid action ids", async () => {
  await withWebBundleTestFixture("console.log(`action:${process.argv[2]}`);\n", async ({ bundleRoot, workspaceRoot }) => {
    const events = [];

    const report = await runBundleTest(
      bundleRoot,
      { steps: [{ kind: "action", actionID: "missing-action" }] },
      { workspaceURL: workspaceRoot, progressHandler: (event) => events.push(event) },
    );

    assert.equal(report.status, "failed");
    assert.equal(report.summary.failed, 1);
    assert.match(report.steps[0].error, /Unknown action: missing-action/);
    assert(events.some((event) => event.type === "message" && /Unknown action: missing-action/.test(event.text)));
  });
});

test("web bundle test runner skips remaining steps after failure", async () => {
  await withWebBundleTestFixture("console.log(`action:${process.argv[2]}`);\n", async ({ bundleRoot, workspaceRoot }) => {
    const report = await runBundleTest(
      bundleRoot,
      {
        steps: [
          { kind: "action", actionID: "say-hello" },
          { kind: "action", actionID: "say-hello" },
        ],
      },
      { workspaceURL: workspaceRoot },
    );

    assert.equal(report.status, "failed");
    assert.equal(report.summary.failed, 1);
    assert.equal(report.summary.skipped, 1);
    assert.equal(report.steps[1].status, "skipped");
  });
});

test("web bundle test runner continues after failure when requested", async () => {
  await withWebBundleTestFixture("console.log(`action:${process.argv[2]}`);\n", async ({ bundleRoot, workspaceRoot }) => {
    const report = await runBundleTest(
      bundleRoot,
      {
        inputs: { fieldValues: { sample: "Ada" } },
        steps: [
          {
            kind: "action",
            actionID: "say-hello",
            requiredOutput: ["not present"],
            continueOnFailure: true,
          },
          { kind: "action", actionID: "say-hello", requiredOutput: ["action:Ada"] },
        ],
      },
      { workspaceURL: workspaceRoot },
    );

    assert.equal(report.status, "failed");
    assert.equal(report.summary.failed, 1);
    assert.equal(report.summary.passed, 1);
    assert.equal(report.summary.skipped, 0);
    assert.equal(report.steps[1].status, "passed");
  });
});

test("web bundle test runner reports missing plan files as read failures", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "gfc-web-bundle-plan-"));
  try {
    await assert.rejects(
      loadBundleTestPlan(path.join(root, "missing.json")),
      /Could not read bundle test plan/,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("web bundle test runner reports invalid plan JSON as parse failures", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "gfc-web-bundle-plan-"));
  const planPath = path.join(root, "invalid.json");
  try {
    await writeFile(planPath, "{", "utf8");

    await assert.rejects(
      loadBundleTestPlan(planPath),
      /Could not parse bundle test plan/,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("web bundle test runner omits setup exit code when setup times out before completion", async () => {
  await withWebBundleTestFixture("console.log(`action:${process.argv[2]}`);\n", async ({ bundleRoot, workspaceRoot }) => {
    const report = await runBundleTest(
      bundleRoot,
      { steps: [{ kind: "setup", timeoutSeconds: 1 }] },
      {
        workspaceURL: workspaceRoot,
        processManager: {
          runProcess: async () => {
            throw new Error("command timed out after 1ms");
          },
        },
      },
    );

    assert.equal(report.status, "failed");
    assert.equal(report.steps[0].exitCode, null);
    assert.equal(report.steps[0].timedOut, true);
  });
});

test("web bundle test runner expands bundle tokens in row values", async () => {
  await withWebBundleTestFixture("console.log(`action:${process.argv[2]}`);\n", async ({ bundleRoot, workspaceRoot }) => {
    const rowRef = path.join(workspaceRoot, "reference", "genomes", "hg19.fa.gz");

    const report = await runBundleTest(
      bundleRoot,
      {
        steps: [
          {
            kind: "action",
            actionID: "show-row-ref",
            controlID: "refs",
            rowValues: { ref: "{{bundleWorkspace}}/reference/genomes/hg19.fa.gz" },
            requiredOutput: [`action:${rowRef}`],
          },
        ],
      },
      { workspaceURL: workspaceRoot },
    );

    assert.equal(report.status, "passed");
    assert.equal(report.steps[0].output, `action:${rowRef}\n`);
  });
});

async function withWebBundleTestFixture(actionScript, callback) {
  const root = await mkdtemp(path.join(tmpdir(), "gfc-web-bundle-test-"));
  const bundleRoot = path.join(root, "Bundle");
  const workspaceRoot = path.join(root, "Workspace");
  await mkdir(path.join(bundleRoot, "scripts"), { recursive: true });
  await writeFile(path.join(bundleRoot, "scripts", "action.mjs"), actionScript, "utf8");
  await writeFile(path.join(bundleRoot, "scripts", "setup.sh"), "printf 'setup-ok\\n'\\n", "utf8");
  await writeFile(
    path.join(bundleRoot, "manifest.json"),
    JSON.stringify({
      id: "dev.guiforcli.web-bundle-test-fixture",
      displayName: "Web Bundle Test Fixture",
      summary: "Fixture bundle.",
      setup: {
        steps: [
          { id: "setup", kind: "setupScript", label: "Setup", value: "scripts/setup.sh" },
        ],
      },
      pages: [
        {
          id: "main",
          title: "Main",
          summary: "Main page.",
          sections: [
            {
              id: "inputs",
              controls: [
                { id: "sample", label: "Sample", kind: "text" },
                {
                  id: "refs",
                  label: "Refs",
                  kind: "libraryList",
                  rowActions: [
                    {
                      id: "show-row-ref",
                      title: "Show row ref",
                      command: {
                        executable: process.execPath,
                        arguments: ["{{bundleRoot}}/scripts/action.mjs", "{{row.ref}}"],
                      },
                    },
                  ],
                },
              ],
              actions: [
                {
                  id: "say-hello",
                  title: "Say hello",
                  command: {
                    executable: process.execPath,
                    arguments: ["{{bundleRoot}}/scripts/action.mjs", "{{sample}}"],
                  },
                },
                {
                  id: "show-row-ref",
                  title: "Section action with same id as row action",
                  command: {
                    executable: process.execPath,
                    arguments: ["{{bundleRoot}}/scripts/action.mjs", "section"],
                  },
                },
              ],
            },
          ],
        },
      ],
    }),
    "utf8",
  );

  try {
    return await callback({ bundleRoot, workspaceRoot });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

async function dataSourceFixture(script) {
  const root = await mkdtemp(path.join(tmpdir(), "gfc-web-bundle-data-source-"));
  const bundleRoot = path.join(root, "Bundle");
  await mkdir(path.join(bundleRoot, "scripts"), { recursive: true });
  await writeFile(path.join(bundleRoot, "scripts", "list.py"), script, "utf8");
  const dataSource = { path: "scripts/list.py", arguments: ["{{library}}"] };
  await writeFile(
    path.join(bundleRoot, "manifest.json"),
    JSON.stringify({
      id: "dev.guiforcli.web-bundle-data-source-fixture",
      displayName: "Data Source Fixture",
      summary: "Fixture bundle.",
      pages: [
        {
          id: "main",
          title: "Main",
          summary: "Main page.",
          sections: [
            {
              id: "inputs",
              controls: [
                { id: "genome", label: "Genome", kind: "dropdown", dataSource },
                {
                  id: "settings",
                  label: "Settings",
                  kind: "configEditor",
                  settings: [
                    { id: "library", key: "library", label: "Library", kind: "path", value: "/from-setting" },
                    { id: "genome", key: "genome", label: "Genome", kind: "dropdown", dataSource },
                  ],
                },
              ],
            },
          ],
        },
      ],
    }),
    "utf8",
  );
  return { root, bundleRoot, workspaceRoot: path.join(root, "Workspace") };
}

const listGenomes = [
  "import json, sys",
  "print(json.dumps({'options': [{'id': sys.argv[1] + '/hg19.fa.gz', 'title': 'hg19'}]}))",
  "",
].join("\n");

test("web bundle test runner loads data sources as the app does", async () => {
  const { root, bundleRoot, workspaceRoot } = await dataSourceFixture(listGenomes);
  const location = { pageID: "main", sectionID: "inputs", controlID: "genome" };
  try {
    const report = await runBundleTest(
      bundleRoot,
      {
        inputs: { fieldValues: { library: "/refs" } },
        steps: [
          // Checked against what the script printed (json.dumps spacing).
          { kind: "dataSource", ...location, minimumOptions: 1, requiredOutput: ['"options": [', "/refs/hg19.fa.gz"] },
          // A config setting sees its editor's settings over fields, as in the app.
          {
            kind: "dataSource", ...location, controlID: "settings", settingID: "genome",
            minimumOptions: 1, requiredOutput: ["/from-setting/hg19.fa.gz"],
          },
          { kind: "dataSource", ...location, minimumOptions: 2, continueOnFailure: true },
          { kind: "dataSource", ...location, controlID: "missing" },
        ],
      },
      { workspaceURL: workspaceRoot },
    );
    assert.deepEqual(report.steps.map((step) => step.status), ["passed", "passed", "failed", "failed"]);
    assert.match(report.steps[0].command, /list\.py$/);
    assert.match(report.steps[2].error, /at least 2 options but the data source offered 1/);
    assert.match(report.steps[3].error, /missing/);
  }
  finally {
    await rm(root, { recursive: true, force: true });
  }
});

for (const [name, script, error] of [
  ["prints nothing", "pass\n", /did not print valid JSON/],
  ["prints options that are not a list", "print('{\"options\": \"hg19\"}')\n", /options are not a list/],
  ["prints a JSON array", "print('[]')\n", /did not print a JSON object/],
]) {
  test(`web bundle test runner fails a data source that ${name}`, async () => {
    const { root, bundleRoot, workspaceRoot } = await dataSourceFixture(script);
    try {
      const report = await runBundleTest(
        bundleRoot,
        { steps: [{ kind: "dataSource", pageID: "main", sectionID: "inputs", controlID: "genome" }] },
        { workspaceURL: workspaceRoot },
      );
      assert.equal(report.status, "failed");
      assert.match(report.steps[0].error, error);
    }
    finally {
      await rm(root, { recursive: true, force: true });
    }
  });
}

test("web bundle test runner enforces a data source step's timeout", async () => {
  const { root, bundleRoot, workspaceRoot } = await dataSourceFixture("import time\ntime.sleep(10)\n");
  try {
    const report = await runBundleTest(
      bundleRoot,
      { steps: [{ kind: "dataSource", pageID: "main", sectionID: "inputs", controlID: "genome", timeoutSeconds: 0.5 }] },
      { workspaceURL: workspaceRoot },
    );
    assert.equal(report.status, "failed");
    assert.equal(report.steps[0].timedOut, true);
    assert(report.steps[0].durationSeconds < 5);
  }
  finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("web bundle test runner fails a data source that exits non-zero", async () => {
  const { root, bundleRoot, workspaceRoot } = await dataSourceFixture("import sys\nsys.exit('no catalog')\n");
  try {
    const report = await runBundleTest(
      bundleRoot,
      { steps: [{ kind: "dataSource", pageID: "main", sectionID: "inputs", controlID: "genome" }] },
      { workspaceURL: workspaceRoot },
    );
    assert.equal(report.status, "failed");
    assert.match(report.steps[0].error, /exited 1: no catalog/);
  }
  finally {
    await rm(root, { recursive: true, force: true });
  }
});
