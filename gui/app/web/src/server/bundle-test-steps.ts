import { runActionTestStep } from "./bundle-test-action-step.js";
import { runDataSourceTestStep } from "./bundle-test-data-source-step.js";
import { runSetupTestStep } from "./bundle-test-setup-step.js";
import type { BundleManifest } from "../../../shared/types.js";
import type { BundleTestRuntime, BundleTestStep, BundleTestStepOptions, BundleTestStepReport } from "./bundle-test-types.js";

export async function runBundleTestStep(
    step: BundleTestStep,
    index: number,
    manifest: BundleManifest,
    workspaceRoot: string,
    runtime: BundleTestRuntime,
    options: BundleTestStepOptions,
): Promise<BundleTestStepReport> {
    switch (step.kind) {
        case "setup":
            return runSetupTestStep(step, index, manifest, workspaceRoot, options);
        case "action":
            return runActionTestStep(step, index, manifest, workspaceRoot, runtime, options);
        case "dataSource":
            return runDataSourceTestStep(step, index, manifest, workspaceRoot, runtime, options);
        default:
            throw new Error(`Unsupported bundle test step kind: ${step.kind}`);
    }
}
