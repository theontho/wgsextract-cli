import type { CommandContext, RunProcess } from "../../../shared/types.js";

/** Plan inputs as written in plan JSON; values are expanded to strings before use. */
export type BundleTestInputs = {
    fieldValues?: Record<string, unknown>;
    configValues?: Record<string, unknown>;
    checkedOptions?: Record<string, unknown>;
};

export type BundleTestStep = {
    kind: string;
    id?: string;
    actionID?: string;
    pageID?: string;
    sectionID?: string;
    controlID?: string;
    /** dataSource steps: a config setting's data source inside controlID. */
    settingID?: string;
    rowID?: string;
    rowValues?: Record<string, unknown>;
    inputs?: BundleTestInputs;
    expectedExitCodes?: Array<number | null>;
    requiredOutput?: string[];
    forbiddenOutput?: string[];
    /** dataSource steps: the fewest options the data source must offer. */
    minimumOptions?: number;
    timeoutSeconds?: number;
    continueOnFailure?: boolean;
};

export type BundleTestPlan = {
    name?: string;
    inputs?: BundleTestInputs;
    steps?: BundleTestStep[];
};

export type BundleTestEvent = { type: "message" | "command-output"; text: string };

export type BundleTestEmit = (event: BundleTestEvent) => void;

export type BundleTestStepOptions = {
    dryRun: boolean;
    emit: BundleTestEmit;
    runProcess: RunProcess;
};

export type BundleTestRuntime = { context: CommandContext };

export type BundleTestStepStatus = "passed" | "failed" | "skipped";

export type BundleTestStepReport = {
    index: number;
    id: string | undefined;
    kind: string;
    actionID: string | undefined;
    status: BundleTestStepStatus;
    command?: string | undefined;
    exitCode?: number | null | undefined;
    timedOut?: boolean;
    startedAt: string;
    finishedAt: string;
    durationSeconds: number;
    output: string;
    error: string | null;
};

export type BundleTestOptions = {
    workspaceURL?: string;
    bootstrapConfig?: boolean;
    dryRun?: boolean;
    runSetup?: boolean;
    progressHandler?: BundleTestEmit;
    processManager?: { runProcess: RunProcess };
    maxOutputBytes?: number;
    maxErrorBytes?: number;
};
