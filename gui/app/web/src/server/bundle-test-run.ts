import path from "node:path";
import { loadManifestFromRoot } from "./bundle-loader.js";
import { createProcessManager } from "./process-runner.js";
import { prepareBundleWorkspace } from "./workspace.js";
import { makeRuntime } from "./bundle-test-runtime.js";
import { runBundleTestStep } from "./bundle-test-steps.js";
import { emitStepFinished, skippedReport, stepDescription, timestamp } from "./bundle-test-report.js";
import type { BundleTestOptions, BundleTestPlan, BundleTestStepReport } from "./bundle-test-types.js";

export async function runBundleTest(bundleURL: string, plan: BundleTestPlan, options: BundleTestOptions = {}) {
    const startedAt = timestamp();
    const sourceRoot = path.resolve(bundleURL);
    const sourceManifest = await loadManifestFromRoot(sourceRoot);
    const workspaceRoot = await prepareBundleWorkspace(sourceManifest, sourceRoot, options.workspaceURL);
    const manifest = await loadManifestFromRoot(workspaceRoot);
    const runtime = await makeRuntime(manifest, workspaceRoot, plan.inputs ?? {}, options.bootstrapConfig !== false);
    const messages: string[] = [
        options.workspaceURL
            ? `[bundle] Using test workspace: ${workspaceRoot}`
            : `[bundle] Using bundle workspace: ${workspaceRoot}`,
    ];
    const emit = options.progressHandler ?? (() => { });
    const steps = plan.steps ?? [];

    emit({ type: "message", text: `Bundle test started: ${plan.name ?? manifest.displayName} (${steps.length} steps)` });
    for (const message of messages) {
        emit({ type: "message", text: message });
    }

    const processManager = options.processManager ?? createProcessManager({
        maxOutputBytes: options.maxOutputBytes ?? 1_048_576,
        maxErrorBytes: options.maxErrorBytes ?? 65_536,
    });
    const reports: BundleTestStepReport[] = [];
    let skipRemaining = false;
    for (const [offset, step] of steps.entries()) {
        const index = offset + 1;
        if (skipRemaining) {
            emit({ type: "message", text: `Step ${index}/${steps.length} skipped after a previous failure.` });
            reports.push(skippedReport(step, index, "Skipped after a previous failure."));
            continue;
        }

        emit({ type: "message", text: `Step ${index}/${steps.length} started: ${stepDescription(step)}` });
        const report = await runBundleTestStep(step, index, manifest, workspaceRoot, runtime, {
            dryRun: Boolean(options.dryRun),
            emit,
            runProcess: processManager.runProcess,
        });
        reports.push(report);
        emitStepFinished(report, steps.length, emit);
        if (report.status === "failed" && !step.continueOnFailure) {
            skipRemaining = true;
        }
    }

    const summary = {
        total: reports.length,
        passed: reports.filter((report) => report.status === "passed").length,
        failed: reports.filter((report) => report.status === "failed").length,
        skipped: reports.filter((report) => report.status === "skipped").length,
    };
    return {
        planName: plan.name,
        bundleID: manifest.id,
        bundleName: manifest.displayName,
        bundleVersion: manifest.version,
        bundleRoot: workspaceRoot,
        status: summary.failed === 0 ? "passed" : "failed",
        startedAt,
        finishedAt: timestamp(),
        summary,
        messages,
        steps: reports,
    };
}
