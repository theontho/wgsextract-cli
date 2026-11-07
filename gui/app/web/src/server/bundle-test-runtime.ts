import { checkedOptionsForContext } from "../../../shared/rendering.js";
import type { BundleManifest, Labels, StringMap, ValueMap } from "../../../shared/types.js";
import { bootstrapConfigFiles, emptyBundleState, initialCheckedOptions, initialConfigFilePaths, initialConfigValues, initialFieldValues } from "./config-store.js";
import { expandPathTokens } from "./paths.js";
import type { BundleTestInputs, BundleTestRuntime } from "./bundle-test-types.js";

export type ExpandedInputs = {
    fieldValues: StringMap;
    configValues: StringMap;
    checkedOptions: Record<string, string[]>;
};

type RuntimeInputs = {
    fieldValues: ValueMap;
    configValues: ValueMap;
    checkedOptions: Record<string, string[]>;
};

export async function makeRuntime(manifest: BundleManifest, workspaceRoot: string, planInputs: BundleTestInputs, bootstrapConfig: boolean): Promise<BundleTestRuntime> {
    const bundleState = emptyBundleState();
    const configFilePaths = initialConfigFilePaths(manifest, bundleState, workspaceRoot);
    if (bootstrapConfig) {
        await bootstrapConfigFiles(manifest, workspaceRoot, configFilePaths);
    }
    const initialConfig = await initialConfigValues(manifest, configFilePaths, workspaceRoot);
    const baseInputs: RuntimeInputs = {
        fieldValues: initialFieldValues(manifest, initialConfig, bundleState),
        configValues: initialConfig,
        checkedOptions: initialCheckedOptions(manifest, initialConfig, bundleState),
    };
    const inputs = mergeInputs(baseInputs, expandInputs(planInputs, workspaceRoot));
    return {
        context: {
            fieldValues: inputs.fieldValues,
            checkedOptions: checkedOptionsForContext(inputs.checkedOptions),
            configValues: inputs.configValues,
            rowValues: {},
            bundleRootPath: workspaceRoot,
            placeholderLabels: placeholderLabels(manifest),
        },
    };
}

function mergeInputs(base: RuntimeInputs, overrides: ExpandedInputs): RuntimeInputs {
    return {
        fieldValues: { ...base.fieldValues, ...overrides.fieldValues },
        configValues: { ...base.configValues, ...overrides.configValues },
        checkedOptions: { ...base.checkedOptions, ...overrides.checkedOptions },
    };
}

export function expandInputs(inputs: BundleTestInputs, workspaceRoot: string): ExpandedInputs {
    return {
        fieldValues: expandValueRecord(inputs.fieldValues ?? {}, workspaceRoot),
        configValues: expandValueRecord(inputs.configValues ?? {}, workspaceRoot),
        checkedOptions: Object.fromEntries(Object.entries(inputs.checkedOptions ?? {}).map(([key, values]) => [
            key,
            checkedOptionValues(values, key, workspaceRoot),
        ])),
    };
}

function checkedOptionValues(values: unknown, key: string, workspaceRoot: string): string[] {
    if (!Array.isArray(values)) {
        throw new Error(`checkedOptions.${key} must be an array.`);
    }
    return values.map((value) => expandPathTokens(String(value), workspaceRoot));
}

export function expandValueRecord(values: Record<string, unknown>, workspaceRoot: string): StringMap {
    return Object.fromEntries(Object.entries(values).map(([key, value]) => [
        key,
        expandPathTokens(String(value), workspaceRoot),
    ]));
}

function placeholderLabels(manifest: BundleManifest): Labels {
    const labels: Labels = {};
    for (const page of manifest.pages ?? []) {
        for (const section of page.sections ?? []) {
            for (const control of section.controls ?? []) {
                labels[control.id] = control.label;
                for (const setting of control.settings ?? []) {
                    labels[setting.id] = setting.label;
                    if (setting.key != null) {
                        labels[setting.key] = setting.label;
                        labels[`${control.id}.${setting.key}`] = setting.label;
                    }
                    labels[`${control.id}.${setting.id}`] = setting.label;
                }
            }
        }
    }
    return labels;
}
