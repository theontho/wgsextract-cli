import type { ConditionSpec, ConfirmationSpec, LooseRecord } from "../../../shared/types.js";

/** A bundle manifest or page that breaks a rule the app relies on; `path` names the offending field. */
export class BundleValidationError extends Error {
    constructor(message: string) {
        super(message);
        this.name = "BundleValidationError";
    }
}

const CONFIG_FILE_PREFIXES = [
    "{{bundleRoot}}/",
    "{{bundleWorkspace}}/",
    "{{home}}/",
    "{{configHome}}/",
    "{{userConfig}}/",
    "{{applicationSupport}}/",
    "{{appConfig}}/",
    "~/",
];
const graphemes = new Intl.Segmenter(undefined, { granularity: "grapheme" });

export function requireNonEmpty(value: unknown, path: string): void {
    if (typeof value !== "string" || !value.trim()) {
        throw new BundleValidationError(`Required field is empty: ${path}`);
    }
}

export function validateUniqueIDs(values: ReadonlyArray<{ id?: unknown }> | undefined, path: string): void {
    validateUniqueValues((values ?? []).map((value) => value.id).filter((id) => id != null).map(String), path);
}

export function validateUniqueValues(values: readonly string[], path: string): void {
    const seen = new Set<string>();
    for (const value of values) {
        if (seen.has(value)) {
            throw new BundleValidationError(`Duplicate id '${value}' at ${path}.`);
        }
        seen.add(value);
    }
}

export function validateRelativePath(value: unknown, path: string): void {
    const trimmed = String(value ?? "").trim();
    const normalized = trimmed.replaceAll("\\", "/");
    if (normalized.startsWith("/") || /^[A-Za-z]:/.test(normalized) || normalized.includes("..")) {
        throw new BundleValidationError(`Invalid relative path at ${path}: ${String(value)}`);
    }
}

export function validateBundledScriptPath(value: unknown, path: string): void {
    validateRelativePath(value, path);
    const trimmed = String(value ?? "").trim();
    if (trimmed.startsWith("~") || trimmed.includes("{{") || trimmed.includes("}}")) {
        throw new BundleValidationError(`Invalid relative path at ${path}: ${String(value)}`);
    }
}

export function validateConfigFilePath(value: unknown, path: string): void {
    const trimmed = String(value ?? "").trim();
    const hasAllowedPrefix = CONFIG_FILE_PREFIXES.some((prefix) => trimmed.startsWith(prefix));
    if (trimmed.startsWith("/") || trimmed.includes("..") || (trimmed.startsWith("{{") && !hasAllowedPrefix)) {
        throw new BundleValidationError(`Invalid relative path at ${path}: ${String(value)}`);
    }
}

export function validateTextIcon(value: unknown, path: string): void {
    if (value == null) {
        return;
    }
    requireNonEmpty(value, path);
    if ([...graphemes.segment(String(value))].length > 2) {
        throw new BundleValidationError(`Text icon at ${path} must be 1 or 2 characters: ${String(value)}`);
    }
}

export function validateDataSource(value: LooseRecord, path: string): void {
    requireNonEmpty(value.path, `${path}.path`);
    validateBundledScriptPath(value.path, `${path}.path`);
    if (value.workingDirectory != null) {
        validateBundledScriptPath(value.workingDirectory, `${path}.workingDirectory`);
    }
}

export function validateConditions(conditions: readonly ConditionSpec[] | undefined, path: string): void {
    for (const [index, condition] of (conditions ?? []).entries()) {
        const conditionPath = `${path}.${index}`;
        requireNonEmpty(condition.placeholder, `${conditionPath}.placeholder`);
        const hasTest = ["equals", "notEquals", "exists", "lessThan", "lessThanOrEqual", "greaterThan", "greaterThanOrEqual"]
            .some((key) => condition[key] != null)
            || (condition.in?.length ?? 0) > 0
            || (condition.notIn?.length ?? 0) > 0;
        if (!hasTest) {
            throw new BundleValidationError(`Required field is empty: ${conditionPath}`);
        }
    }
}

export function validateConfirmation(value: ConfirmationSpec | undefined, path: string): void {
    if (value == null) {
        return;
    }
    requireNonEmpty(value.title, `${path}.title`);
    requireNonEmpty(value.confirmButtonTitle, `${path}.confirmButtonTitle`);
    requireNonEmpty(value.cancelButtonTitle, `${path}.cancelButtonTitle`);
    for (const key of ["requiredText", "prompt"] as const) {
        if (value[key] != null) {
            requireNonEmpty(value[key], `${path}.${key}`);
        }
    }
}
