import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import test from "node:test";
import { supervise } from "../scripts/dev.mjs";

function waitingChild() {
  return spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore" });
}

test("development runner stops the sibling when one child fails", { timeout: 15000 }, async () => {
  const sibling = waitingChild();
  const failing = spawn(process.execPath, ["-e", "process.exit(7)"], { stdio: "ignore" });
  assert.equal(await supervise([sibling, failing]).done, 7);
  assert.ok(sibling.exitCode !== null || sibling.signalCode !== null);
});

test("development runner stops siblings on a spawn failure", { timeout: 15000 }, async () => {
  const sibling = waitingChild();
  const missing = spawn("gui-for-cli-nonexistent-test-executable", [], { stdio: "ignore" });
  assert.equal(await supervise([sibling, missing]).done, 1);
  assert.ok(sibling.exitCode !== null || sibling.signalCode !== null);
});

test("development runner's explicit stop waits for child cleanup", { timeout: 15000 }, async () => {
  const children = [waitingChild(), waitingChild()];
  const runner = supervise(children);
  runner.stop(130);
  assert.equal(await runner.done, 130);
  for (const child of children) assert.ok(child.exitCode !== null || child.signalCode !== null);
});
