import { allControls } from "./rendering-controls.js";
import type { BundleManifest, CommandContext, Labels, LooseRecord, StateValue, StringMap, ValueMap } from "./types.js";

type CheckedOptionsInput = Record<string, Set<string> | string[] | string | null | undefined>;
type StateLike = {
    fieldValues?: ValueMap;
    checkedOptions?: CheckedOptionsInput;
    configValues?: ValueMap;
    bundleRootPath?: string;
    manifest?: BundleManifest | null;
    homePath?: string;
} & LooseRecord;

export function commandContextFromState(state: StateLike, rowValues: ValueMap = {}, sectionValues: ValueMap = {}): CommandContext {
    const context: CommandContext = {
        fieldValues: { ...(state.fieldValues ?? {}), ...sectionValues },
        checkedOptions: checkedOptionsForContext(state.checkedOptions ?? {}),
        configValues: { ...(state.configValues ?? {}), ...(state.fieldValues ?? {}), ...sectionValues },
        rowValues,
        placeholderLabels: placeholderLabelsFromManifest(state.manifest),
    };
    if (state.bundleRootPath != null) {
        context.bundleRootPath = state.bundleRootPath;
    }
    if (state.homePath != null) {
        context.homePath = state.homePath;
    }
    return context;
}
function placeholderLabelsFromManifest(manifest: BundleManifest | null | undefined): Labels {
    const labels: Labels = {};
    for (const control of allControls(manifest ?? {})) {
        if (control.label) {
            labels[control.id] = control.label;
        }
        for (const setting of control.settings ?? []) {
            if (!setting.label) {
                continue;
            }
            labels[setting.id] = setting.label;
            if (setting.key) {
                labels[setting.key] = setting.label;
                labels[`${control.id}.${setting.key}`] = setting.label;
            }
            labels[`${control.id}.${setting.id}`] = setting.label;
        }
    }
    return labels;
}

export function contextValue(context: CommandContext, placeholder: string): StateValue {
    if (placeholder === "bundleRoot" || placeholder === "bundleWorkspace") {
        return context.bundleRootPath;
    }
    if (placeholder === "home") {
        return context.homePath;
    }
    if (placeholder.startsWith("row.")) {
        return context.rowValues?.[placeholder.slice(4)];
    }
    if (placeholder.startsWith("config.")) {
        return context.configValues?.[placeholder.slice(7)];
    }
    const computed = computedFileStateValue(context, placeholder);
    if (computed != null) {
        return computed;
    }
    return (context.rowValues?.[placeholder] ??
        context.checkedOptions?.[placeholder] ??
        context.fieldValues?.[placeholder] ??
        context.configValues?.[placeholder]);
}

export function checkedOptionsForContext(checkedOptions: CheckedOptionsInput): StringMap {
    return Object.fromEntries(Object.entries(checkedOptions).map(([key, selected]) => [
        key,
        selected instanceof Set || Array.isArray(selected)
            ? normalizeSelectedIDs(selected).sort().join(",")
            : selected == null
                ? ""
                : String(selected),
    ]));
}
export function normalizeSelectedIDs(value: Set<unknown> | unknown[] | unknown): string[] {
    if (value instanceof Set) {
        return [...value].map(String);
    }
    if (Array.isArray(value)) {
        return value.map(String);
    }
    return String(value ?? "")
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
}

function computedFileStateValue(context: CommandContext, placeholder: string): StateValue {
    const separator = placeholder.lastIndexOf(".");
    if (separator <= 0 || separator >= placeholder.length - 1) {
        return undefined;
    }
    const fieldID = placeholder.slice(0, separator);
    const property = placeholder.slice(separator + 1);
    const rawPath = context.fieldValues?.[fieldID] ?? context.configValues?.[fieldID];
    const serverComputed = context.fileStateValues?.[placeholder];
    if (serverComputed != null) {
        return serverComputed;
    }
    switch (property) {
        case "pathExtension": {
            const name = String(rawPath ?? "").split(/[\\/]/).pop() ?? "";
            const dot = name.lastIndexOf(".");
            return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
        }
        default:
            return undefined;
    }
}
