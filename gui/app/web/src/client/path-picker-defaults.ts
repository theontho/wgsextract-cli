import { commandContextFromState, interpolate } from "../../../shared/rendering.js";
import type { WebUIState } from "../../../shared/types.js";

/** A control, config setting, or ad-hoc request (the bundle picker) that asks for a path. */
export interface PathPickerSpec {
    id?: string;
    key?: string;
    label?: string;
    tooltip?: string;
    defaultDirectory?: unknown;
    [key: string]: unknown;
}

export function pathPickerDefaultPath(spec: PathPickerSpec | undefined, currentValue: unknown, state: WebUIState) {
    const currentPath = String(currentValue ?? "").trim();
    if (currentPath) {
        return currentPath;
    }
    const defaultDirectory = String(spec?.defaultDirectory ?? "").trim();
    return defaultDirectory ? interpolate(defaultDirectory, commandContextFromState(state)) : "";
}
