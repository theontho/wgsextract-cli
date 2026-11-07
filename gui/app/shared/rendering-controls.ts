import type { BundleManifest, ConfigSetting, ControlSpec, LooseRecord, ValueMap } from "./types.js";

export function initialFieldValues(manifest: BundleManifest): ValueMap {
    const values: ValueMap = {};
    for (const control of allControls(manifest)) {
        if (persistsFieldValue(control.kind)) {
            values[control.id] = control.value ?? values[control.id] ?? "";
        }
    }
    return values;
}
export function initialCheckedOptions(manifest: BundleManifest): Record<string, Set<string>> {
    const values: Record<string, Set<string>> = {};
    for (const control of allControls(manifest)) {
        if (control.kind === "checkboxGroup") {
            values[control.id] = new Set((control.options ?? []).filter((option) => option.selected).map((option) => option.id));
        }
    }
    return values;
}
export function initialConfigValues(manifest: BundleManifest): ValueMap {
    const values: ValueMap = {};
    for (const control of configEditorControls(manifest)) {
        for (const setting of control.settings ?? []) {
            values[configValueKey(control, setting)] = setting.value ?? "";
        }
    }
    return values;
}
export function configEditorControls(manifest: Partial<BundleManifest> | LooseRecord): ControlSpec[] {
    return allControls(manifest).filter((control) => control.kind === "configEditor");
}
export function allControls(manifest: Partial<BundleManifest> | LooseRecord): ControlSpec[] {
    const pages = (manifest as Partial<BundleManifest>).pages;
    if (!Array.isArray(pages)) {
        return [];
    }
    return pages.flatMap((page) => (page.sections ?? []).flatMap((section) => section.controls ?? []));
}
export function configValueKey(control: Pick<ControlSpec, "id"> | LooseRecord, setting: Pick<ConfigSetting, "id"> | LooseRecord): string {
    return `${String(control.id)}.${String(setting.id)}`;
}

export function persistsFieldValue(kind: string): boolean {
    return ["text", "path", "dropdown", "toggle"].includes(kind);
}
