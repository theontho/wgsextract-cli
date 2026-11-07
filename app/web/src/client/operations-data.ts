import { checkedOptionsForContext } from "../../../shared/rendering.js";
import { api } from "./api.js";
import { errorMessage } from "./model.js";
import { scheduleRender } from "./rerender.js";
import { state } from "./state.js";
import type { ActionReference, ActionSpec, CommandContext, DataSourcePayload, FileStateResponse, ManifestLocation, PrecheckResult } from "../../../shared/types.js";

const precheckLabelKeys = [
    "actionPrecheckDiskSpaceTitle",
    "actionPrecheckDiskSpaceInfoTitle",
    "actionPrecheckDiskSpaceMessageFormat",
    "actionPrecheckDiskSpaceInfoFormat",
];

/** The localized strings a precheck message is built from. */
export function precheckLabels() {
    return Object.fromEntries(precheckLabelKeys.flatMap((key) => {
        const value = state.labels[key];
        return typeof value === "string" ? [[key, value]] : [];
    }));
}

export function ensureDataSource(key: string, source: ManifestLocation, context: CommandContext) {
    if (state.dataSourcePayloads.has(key) || state.dataSourceErrors.has(key) || state.loadingDataSources.has(key)) {
        return;
    }
    // Data sources may need what setup installs; runSetup clears them when it ends, which loads them again.
    if (state.setupRun?.status === "running") {
        return;
    }
    state.dataSourceErrors.delete(key);
    state.loadingDataSources.add(key);
    api<DataSourcePayload>("/api/datasource", { method: "POST", body: { source, context } })
        .then((payload) => {
        state.dataSourcePayloads.set(key, payload);
        selectDefaultDataSourceOption(key, payload);
        state.dataSourceErrors.delete(key);
    })
        .catch((error) => {
        state.dataSourceErrors.set(key, errorMessage(error));
    })
        .finally(() => {
        state.loadingDataSources.delete(key);
        scheduleRender();
    });
}
export function ensureActionPrecheck(key: string, reference: ActionReference, context: CommandContext) {
    if (!key || state.actionPrechecks.has(key) || state.actionPrecheckErrors.has(key) || state.loadingActionPrechecks.has(key)) {
        return state.actionPrechecks.get(key) ?? null;
    }
    state.actionPrecheckErrors.delete(key);
    state.loadingActionPrechecks.add(key);
    api<PrecheckResult>("/api/precheck", {
        method: "POST",
        body: { target: { kind: "action", ...reference }, context, labels: precheckLabels() },
    })
        .then((result) => {
        state.actionPrechecks.set(key, result);
        state.actionPrecheckErrors.delete(key);
    })
        .catch((error) => {
        state.actionPrecheckErrors.set(key, errorMessage(error));
    })
        .finally(() => {
        state.loadingActionPrechecks.delete(key);
        scheduleRender();
    });
    return null;
}
export function retryDataSource(key: string) {
    state.dataSourceErrors.delete(key);
    scheduleRender();
}
export function retryActionPrecheck(key: string) {
    state.actionPrecheckErrors.delete(key);
    scheduleRender();
}
export function contextWithFileState(context: CommandContext): CommandContext {
    const key = fileStateKey(context);
    ensureFileState(key, context);
    const fileStateValues = state.fileStateValues.get(key);
    return fileStateValues ? { ...context, fileStateValues } : context;
}
function ensureFileState(key: string, context: CommandContext) {
    if (!key || state.fileStateValues.has(key) || state.loadingFileStates.has(key)) {
        return;
    }
    state.loadingFileStates.add(key);
    api<FileStateResponse>("/api/file-state", { method: "POST", body: { context } })
        .then((result) => {
        state.fileStateValues.set(key, result.values ?? {});
    })
        .catch((error) => {
        console.warn(`Could not resolve file state: ${errorMessage(error)}`);
        state.fileStateValues.set(key, {});
    })
        .finally(() => {
        state.loadingFileStates.delete(key);
        scheduleRender();
    });
}
function fileStateKey(context: CommandContext) {
    return JSON.stringify({
        fieldValues: context.fieldValues,
        configValues: context.configValues,
        rowValues: context.rowValues,
        bundleRootPath: context.bundleRootPath,
    });
}
export function actionPrecheckKey(action: ActionSpec, context: CommandContext) {
    return JSON.stringify({
        actionID: action.id,
        precheck: action.precheck,
        fieldValues: context.fieldValues,
        checkedOptions: checkedOptionsForContext(context.checkedOptions ?? {}),
        configValues: context.configValues,
        rowValues: context.rowValues,
        bundleRootPath: context.bundleRootPath,
    });
}
export function selectDefaultDataSourceOption(key: string, payload: DataSourcePayload) {
    const options = payload.options;
    if (!options?.length) {
        return;
    }
    const defaultValue = (options.find((option) => option.selected) ?? options[0])?.id ?? "";
    if (key.startsWith("control:")) {
        const controlID = key.slice("control:".length);
        const current = String(state.fieldValues[controlID] ?? "").trim();
        if (!current || !options.some((option) => option.id === current)) {
            state.fieldValues[controlID] = defaultValue;
        }
        return;
    }
    if (key.startsWith("setting:")) {
        const configKey = key.slice("setting:".length);
        const current = String(state.configValues[configKey] ?? "").trim();
        if (!current || !options.some((option) => option.id === current)) {
            state.configValues[configKey] = defaultValue;
        }
    }
}
