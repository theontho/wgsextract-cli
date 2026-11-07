import { api } from "./api.js";
import { state } from "./state.js";

export type PersistBundleStateOptions = {
    removeFieldIDs?: string[];
    removeCheckedIDs?: string[];
};

export async function persistBundleState(options: PersistBundleStateOptions = {}) {
    const fieldValues = { ...state.fieldValues };
    for (const id of options.removeFieldIDs ?? []) {
        delete fieldValues[id];
    }
    const checkedOptions = Object.fromEntries(Object.entries(state.checkedOptions).map(([key, selected]) => [
        key,
        [...(selected instanceof Set ? selected : new Set(Array.isArray(selected) ? selected : []))].sort(),
    ]));
    for (const id of options.removeCheckedIDs ?? []) {
        delete checkedOptions[id];
    }
    await api<void>("/api/state/save", {
        method: "POST",
        body: {
            state: {
                localizationCode: state.usingSystemDefaultLocale ? null : state.localizationCode,
                configFilePaths: state.configFilePaths,
                fieldValues,
                checkedOptions,
                selectedPageID: state.activePageID,
                iconSet: state.iconSet,
                colorTheme: state.colorTheme,
                webUIFont: state.webUIFont,
                ...(state.setupRun?.status === "running" ? {} : { setupRun: state.setupRun }),
            },
        },
    });
}
