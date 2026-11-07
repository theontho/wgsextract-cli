import { api } from "./api.js";
import { precheckLabels } from "./operations-data.js";
import { errorMessage } from "./model.js";
import { scheduleRender } from "./rerender.js";
import { state } from "./state.js";
import type { PrecheckResult } from "../../../shared/types.js";

export function setupInstallSizeGB() {
    const value = Number(state.manifest?.setup?.initialInstallSizeGB);
    return Number.isFinite(value) && value > 0 ? value : null;
}
export function setupInstallSizePrecheck() {
    const requiredGB = setupInstallSizeGB();
    if (!requiredGB) {
        return null;
    }
    return { diskSpaceGB: String(requiredGB), diskSpacePath: "{{bundleRoot}}" };
}
export function ensureSetupPreflight() {
    const precheck = setupInstallSizePrecheck();
    if (!precheck) {
        state.setupPreflight = null;
        state.setupPreflightError = "";
        state.setupPreflightKey = "";
        return null;
    }
    const key = JSON.stringify({ precheck, bundleRootPath: state.bundleRootPath });
    if (state.setupPreflightKey !== key) {
        state.setupPreflight = null;
        state.setupPreflightError = "";
        state.loadingSetupPreflight = false;
        state.setupPreflightKey = key;
    }
    if (state.setupPreflight || state.setupPreflightError || state.loadingSetupPreflight) {
        return state.setupPreflight;
    }
    state.loadingSetupPreflight = true;
    api<PrecheckResult>("/api/precheck", {
        method: "POST",
        body: { target: { kind: "setup" }, labels: precheckLabels() },
    })
        .then((result) => {
        state.setupPreflight = result ?? { severity: "info", message: "" };
        state.setupPreflightError = "";
    })
        .catch((error) => {
        state.setupPreflight = null;
        state.setupPreflightError = errorMessage(error);
    })
        .finally(() => {
        state.loadingSetupPreflight = false;
        scheduleRender();
    });
    return null;
}
export async function resolveSetupPreflight() {
    const precheck = setupInstallSizePrecheck();
    if (!precheck) {
        return null;
    }
    try {
        const result = await api<PrecheckResult>("/api/precheck", {
            method: "POST",
            body: { target: { kind: "setup" }, labels: precheckLabels() },
        });
        state.setupPreflight = result;
        state.setupPreflightError = "";
        state.setupPreflightKey = JSON.stringify({ precheck, bundleRootPath: state.bundleRootPath });
        return result;
    }
    catch (error) {
        state.setupPreflight = null;
        state.setupPreflightError = errorMessage(error);
        return null;
    }
}
