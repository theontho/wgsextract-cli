import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const { loadLocalizedBundle } = await import("../dist/web/src/server/bundle-loader.js");
const { loadBundleTestPlan, runBundleTest } = await import("../dist/web/src/server/bundle-test-runner.js");

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const examples = [
  {
    id: "git-helper",
    directory: "GitHelper",
    requiredTools: ["git"],
    outputFiles: [],
  },
  {
    id: "curl-workbench",
    directory: "CurlWorkbench",
    requiredTools: ["curl"],
    outputFiles: ["downloaded-payload.txt"],
  },
  {
    id: "ffmpeg-media",
    directory: "FFmpegMedia",
    requiredTools: ["ffmpeg", "ffprobe"],
    outputFiles: ["test-video.mp4", "test-video-copy.mp4"],
  },
  {
    id: "imagemagick-tools",
    directory: "ImageMagickTools",
    requiredTools: ["magick"],
    outputFiles: ["sample.png", "sample-small.png", "sample-stripped.png"],
  },
];

test("generic example bundles expose multiple pages", async () => {
  for (const example of examples) {
    const bundleRoot = exampleRoot(example);
    const { manifest } = await loadLocalizedBundle("en", repoRoot, bundleRoot, bundleRoot);

    assert.equal(manifest.id, example.id);
    assert.ok(
      manifest.pages.length >= 2,
      `${example.directory} should expose multiple pages`,
    );
  }
});

for (const example of examples) {
  const missingTools = example.requiredTools.filter((tool) => !commandExists(tool));
  test(
    `${example.directory} smoke plan runs real CLI actions`,
    { skip: missingTools.length ? `Missing required tools: ${missingTools.join(", ")}` : false },
    async () => {
      const bundleRoot = exampleRoot(example);
      const plan = await loadBundleTestPlan(path.join(bundleRoot, "test-plans", "smoke.json"));
      const workspaceRoot = await mkdtemp(path.join(tmpdir(), `gfc-${example.id}-`));

      try {
        const report = await runBundleTest(bundleRoot, plan, { workspaceURL: workspaceRoot });

        assert.equal(
          report.status,
          "passed",
          report.steps.map((step) => `${step.id}: ${step.error ?? "ok"}`).join("\n"),
        );
        for (const outputFile of example.outputFiles) {
          assert.ok(
            existsSync(path.join(workspaceRoot, outputFile)),
            `${example.directory} should create ${outputFile}`,
          );
        }
      } finally {
        await rm(workspaceRoot, { recursive: true, force: true });
      }
    },
  );
}

function exampleRoot(example) {
  return path.join(repoRoot, "examples", example.directory);
}

function commandExists(command) {
  return [["--version"], ["-version"]].some((args) => spawnSync(command, args, {
    encoding: "utf8",
    stdio: "ignore",
  }).status === 0);
}
