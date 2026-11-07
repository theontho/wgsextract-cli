import { readFile } from "node:fs/promises";
import { errorMessage } from "./errors.js";
import type { BundleTestPlan } from "./bundle-test-types.js";

export async function loadBundleTestPlan(planPath: string | undefined): Promise<BundleTestPlan> {
    if (!planPath) {
        return { steps: [] };
    }
    let source: string;
    try {
        source = await readFile(planPath, "utf8");
    }
    catch (error) {
        throw new Error(`Could not read bundle test plan ${planPath}: ${errorMessage(error)}`);
    }
    try {
        return JSON.parse(source) as BundleTestPlan;
    }
    catch (error) {
        throw new Error(`Could not parse bundle test plan ${planPath}: ${errorMessage(error)}`);
    }
}
