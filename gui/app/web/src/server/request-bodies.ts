import type { ActionReference, CommandContext, Labels, LooseRecord, ManifestLocation, PrimitiveValue, StringMap, ValueMap } from "../../../shared/types.js";
import { HttpError } from "./errors.js";

export type PrecheckTarget = ({ kind: "action" } & ActionReference) | { kind: "setup" };

export interface BundleStatePatch {
    localizationCode?: string | null;
    configFilePaths?: StringMap;
    fieldValues?: ValueMap;
    checkedOptions?: Record<string, string[]>;
    selectedPageID?: string | null;
    setupRun?: LooseRecord | null;
    iconSet?: string;
    colorTheme?: string;
    webUIFont?: string;
}

export function requestObject(value: unknown, name = "request body"): LooseRecord {
    if (value == null || typeof value !== "object" || Array.isArray(value)) {
        throw invalid(`${name} must be an object`);
    }
    return value as LooseRecord;
}

export function requiredString(record: LooseRecord, key: string): string {
    const value = record[key];
    if (typeof value !== "string" || !value.trim()) {
        throw invalid(`Missing required field: ${key}`);
    }
    return value;
}

export function optionalString(record: LooseRecord, key: string): string | undefined {
    const value = record[key];
    if (value == null) {
        return undefined;
    }
    if (typeof value !== "string") {
        throw invalid(`${key} must be a string`);
    }
    return value;
}

export function parseManifestLocation(value: unknown): ManifestLocation {
    const record = requestObject(value, "location");
    const controlID = optionalString(record, "controlID");
    const settingID = optionalString(record, "settingID");
    return {
        pageID: requiredString(record, "pageID"),
        sectionID: requiredString(record, "sectionID"),
        ...(controlID ? { controlID } : {}),
        ...(settingID ? { settingID } : {}),
    };
}

export function parseActionReference(value: unknown): ActionReference {
    return { ...parseManifestLocation(value), actionID: requiredString(requestObject(value, "action"), "actionID") };
}

export function parsePrecheckTarget(value: unknown): PrecheckTarget {
    const record = requestObject(value, "target");
    if (record.kind === "setup") {
        return { kind: "setup" };
    }
    if (record.kind === "action") {
        return { kind: "action", ...parseActionReference(record) };
    }
    throw invalid("target.kind must be action or setup");
}

/** Accepts only the value maps a command context is built from; server-derived fields are recomputed. */
export function parseCommandContext(value: unknown): CommandContext {
    const record = value == null ? {} : requestObject(value, "context");
    return {
        fieldValues: valueMap(record.fieldValues, "context.fieldValues"),
        checkedOptions: stringMap(record.checkedOptions, "context.checkedOptions"),
        configValues: valueMap(record.configValues, "context.configValues"),
        rowValues: valueMap(record.rowValues, "context.rowValues"),
        placeholderLabels: stringMap(record.placeholderLabels, "context.placeholderLabels") as Labels,
    };
}

export function parseLabels(value: unknown): Labels {
    return stringMap(value, "labels") as Labels;
}

export function parseBundleStatePatch(value: unknown): BundleStatePatch {
    const record = requestObject(value, "state");
    const patch: BundleStatePatch = {};
    for (const key of ["localizationCode", "selectedPageID"] as const) {
        if (Object.hasOwn(record, key)) {
            patch[key] = nullableString(record[key], key);
        }
    }
    for (const key of ["iconSet", "colorTheme", "webUIFont"] as const) {
        const text = optionalString(record, key);
        if (text != null) {
            patch[key] = text;
        }
    }
    if (record.configFilePaths != null) {
        patch.configFilePaths = stringMap(record.configFilePaths, "configFilePaths");
    }
    if (record.fieldValues != null) {
        patch.fieldValues = valueMap(record.fieldValues, "fieldValues");
    }
    if (record.checkedOptions != null) {
        patch.checkedOptions = Object.fromEntries(Object.entries(requestObject(record.checkedOptions, "checkedOptions")).map(([key, selected]) => {
            if (!Array.isArray(selected) || !selected.every((item) => typeof item === "string")) {
                throw invalid(`checkedOptions.${key} must be an array of strings`);
            }
            return [key, selected];
        }));
    }
    if (Object.hasOwn(record, "setupRun")) {
        patch.setupRun = record.setupRun == null ? null : requestObject(record.setupRun, "setupRun");
    }
    return patch;
}

function valueMap(value: unknown, name: string): ValueMap {
    if (value == null) {
        return {};
    }
    const entries = Object.entries(requestObject(value, name));
    for (const [key, item] of entries) {
        if (!isPrimitive(item)) {
            throw invalid(`${name}.${key} must be a string, number, boolean, or null`);
        }
    }
    return Object.fromEntries(entries) as ValueMap;
}

function stringMap(value: unknown, name: string): StringMap {
    if (value == null) {
        return {};
    }
    const entries = Object.entries(requestObject(value, name));
    for (const [key, item] of entries) {
        if (typeof item !== "string") {
            throw invalid(`${name}.${key} must be a string`);
        }
    }
    return Object.fromEntries(entries) as StringMap;
}

function nullableString(value: unknown, name: string): string | null {
    if (value == null) {
        return null;
    }
    if (typeof value !== "string") {
        throw invalid(`${name} must be a string or null`);
    }
    return value;
}

function isPrimitive(value: unknown): value is PrimitiveValue {
    return value === null || ["string", "number", "boolean"].includes(typeof value);
}

function invalid(message: string) {
    return new HttpError(400, message);
}
