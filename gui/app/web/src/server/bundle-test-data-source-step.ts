import { checkedOptionsForContext, configValueKey } from "../../../shared/rendering.js";
import { runDataSourceScript } from "./action-runner.js";
import { errorMessage } from "./errors.js";
import { findConfigEditorControl, resolveDataSource } from "./manifest-lookup.js";
import { resolvePlatformScriptPath } from "./platform-scripts.js";
import { expandInputs } from "./bundle-test-runtime.js";
import { makeReport, outputExpectationFailure } from "./bundle-test-report.js";
import { timeoutRunProcess } from "./bundle-test-timeout.js";
import type { BundleManifest, CommandContext, DataSourcePayload } from "../../../shared/types.js";
import type { BundleTestRuntime, BundleTestStep, BundleTestStepOptions, BundleTestStepReport } from "./bundle-test-types.js";

const defaultTimeoutSeconds = 15;

/** Loads a control's (or config setting's) data source the way the app does when
 *  it shows the page, and checks what it returns: the script must resolve for
 *  this platform, exit 0, print a JSON object, and offer at least
 *  `minimumOptions`. Output checks see exactly what the script printed. */
export async function runDataSourceTestStep(
    step: BundleTestStep,
    index: number,
    manifest: BundleManifest,
    workspaceRoot: string,
    runtime: BundleTestRuntime,
    options: BundleTestStepOptions,
): Promise<BundleTestStepReport> {
    const started = new Date();
    const timeoutState = { timedOut: false };
    let command: string | undefined;
    let output = "";
    try {
        if (!step.pageID || !step.sectionID || !step.controlID) {
            throw new Error("Data source steps must include pageID, sectionID and controlID.");
        }
        const dataSource = resolveDataSource(manifest, {
            pageID: step.pageID,
            sectionID: step.sectionID,
            controlID: step.controlID,
            ...(step.settingID ? { settingID: step.settingID } : {}),
        });
        command = await resolvePlatformScriptPath(String(dataSource.path), workspaceRoot);
        options.emit({ type: "message", text: `$ ${command} (data source)` });
        if (options.dryRun) {
            return makeReport(step, index, started, "skipped", { command });
        }
        const timeoutSeconds = step.timeoutSeconds ?? defaultTimeoutSeconds;
        const result = await runDataSourceScript(
            dataSource,
            stepContext(step, manifest, runtime.context, workspaceRoot),
            workspaceRoot,
            timeoutRunProcess(options.runProcess, timeoutSeconds, timeoutState),
            timeoutSeconds * 1000,
        );
        output = result.stdout;
        options.emit({ type: "command-output", text: output });
        const offered = offeredOptions(parsePayload(result.path, output));
        const minimum = step.minimumOptions ?? 0;
        const error = offered < minimum
            ? `Expected at least ${minimum} options but the data source offered ${offered}.`
            : outputExpectationFailure(output, step);
        return makeReport(step, index, started, error ? "failed" : "passed", { command, exitCode: 0, output, error });
    }
    catch (error) {
        return makeReport(step, index, started, "failed", {
            command,
            output,
            timedOut: timeoutState.timedOut,
            error: errorMessage(error),
        });
    }
}

function parsePayload(sourcePath: string, output: string): DataSourcePayload {
    let payload: unknown;
    try {
        // Unlike the app, no output is not an empty list: the script must print one.
        payload = JSON.parse(output);
    }
    catch (error) {
        throw new Error(`Data source ${sourcePath} did not print valid JSON: ${errorMessage(error)}`);
    }
    if (payload == null || typeof payload !== "object" || Array.isArray(payload)) {
        throw new Error(`Data source ${sourcePath} did not print a JSON object.`);
    }
    return payload as DataSourcePayload;
}

function offeredOptions(payload: DataSourcePayload): number {
    if (payload.options == null) {
        return 0;
    }
    if (!Array.isArray(payload.options)) {
        throw new Error("The data source's options are not a list.");
    }
    return payload.options.length;
}

/** The context the app gives this data source: an ordinary control sees fields
 *  over config (commandContextFromState); a config setting sees its editor's
 *  settings by ID and key, over fields (the client's configDataSourceContext). */
function stepContext(step: BundleTestStep, manifest: BundleManifest, base: CommandContext, workspaceRoot: string): CommandContext {
    const inputs = expandInputs(step.inputs ?? {}, workspaceRoot);
    const fieldValues = { ...base.fieldValues, ...(inputs.fieldValues ?? {}) };
    const configValues = { ...base.configValues, ...(inputs.configValues ?? {}) };
    const checkedOptions = { ...base.checkedOptions, ...checkedOptionsForContext(inputs.checkedOptions ?? {}) };
    if (!step.settingID || !step.controlID) {
        return { ...base, fieldValues, checkedOptions, configValues: { ...configValues, ...fieldValues } };
    }
    const control = findConfigEditorControl(manifest, step.controlID);
    const settingValues = { ...configValues };
    for (const setting of control.settings ?? []) {
        const value = configValues[configValueKey(control, setting)] ?? setting.value ?? "";
        settingValues[setting.id] = value;
        if (setting.key) {
            settingValues[setting.key] = value;
        }
    }
    return { ...base, fieldValues: { ...fieldValues, ...settingValues }, checkedOptions, configValues: settingValues };
}
