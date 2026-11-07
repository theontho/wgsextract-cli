import type { ActionReference, ActionSpec, BundleManifest, BundlePage, ConfigSetting, ControlSpec, LooseRecord, ManifestLocation, PageSection } from "../../../shared/types.js";
import { HttpError } from "./errors.js";

export interface ActionCandidate {
    action: ActionSpec;
    page: BundlePage;
    section: PageSection;
    control?: ControlSpec;
}

export interface ActionCandidateFilter {
    pageID?: string;
    sectionID?: string;
    controlID?: string;
}

/** Actions currently offered by data sources, keyed by `locationKey` of the control that ran them. */
export type DynamicActions = Map<string, ActionSpec[]>;

export function locationKey(location: ManifestLocation): string {
    return JSON.stringify([location.pageID, location.sectionID, location.controlID ?? null, location.settingID ?? null]);
}

/** Section actions and control row actions, optionally narrowed to one page, section, or control. */
export function manifestActionCandidates(manifest: BundleManifest, filter: ActionCandidateFilter = {}, dynamicActions?: DynamicActions): ActionCandidate[] {
    const candidates: ActionCandidate[] = [];
    for (const page of manifest.pages ?? []) {
        if (filter.pageID && page.id !== filter.pageID) {
            continue;
        }
        for (const section of page.sections ?? []) {
            if (filter.sectionID && section.id !== filter.sectionID) {
                continue;
            }
            if (!filter.controlID) {
                candidates.push(...(section.actions ?? []).map((action) => ({ action, page, section })));
            }
            for (const control of section.controls ?? []) {
                if (filter.controlID && control.id !== filter.controlID) {
                    continue;
                }
                const location = { pageID: page.id, sectionID: String(section.id ?? ""), controlID: control.id };
                const rowActions = dynamicActions?.get(locationKey(location)) ?? control.rowActions ?? [];
                candidates.push(...rowActions.map((action) => ({ action, page, section, control })));
            }
        }
    }
    return candidates;
}

export function resolveActionReference(manifest: BundleManifest, reference: ActionReference, dynamicActions?: DynamicActions): ActionSpec {
    const filter = { pageID: reference.pageID, sectionID: reference.sectionID, ...(reference.controlID ? { controlID: reference.controlID } : {}) };
    const matches = manifestActionCandidates(manifest, filter, dynamicActions)
        .filter((candidate) => candidate.action.id === reference.actionID && (reference.controlID != null || candidate.control == null));
    if (matches.length !== 1 || !matches[0]) {
        throw new HttpError(matches.length ? 409 : 404, `${matches.length ? "Ambiguous" : "Unknown"} action ${reference.actionID} in ${reference.pageID}/${reference.sectionID}${reference.controlID ? `/${reference.controlID}` : ""}.`);
    }
    return matches[0].action;
}

/** The data source declared at a section, control, or config setting. */
export function resolveDataSource(manifest: BundleManifest, location: ManifestLocation): LooseRecord {
    const section = findSection(manifest, location);
    let owner: { dataSource?: LooseRecord } = section;
    if (location.controlID) {
        const control = findControlInSection(section, location.controlID);
        owner = location.settingID ? findSetting(control, location.settingID) : control;
    }
    if (!owner.dataSource) {
        throw new HttpError(404, `No data source is declared at ${describe(location)}.`);
    }
    return owner.dataSource;
}

export function findConfigEditorControl(manifest: BundleManifest, controlID: string): ControlSpec {
    const control = (manifest.pages ?? [])
        .flatMap((page) => page.sections ?? [])
        .flatMap((section) => section.controls ?? [])
        .find((candidate) => candidate.id === controlID && candidate.kind === "configEditor");
    if (!control) {
        throw new HttpError(404, `Unknown settings editor: ${controlID}`);
    }
    return control;
}

function findSection(manifest: BundleManifest, location: ManifestLocation): PageSection {
    const page = (manifest.pages ?? []).find((candidate) => candidate.id === location.pageID);
    const section = page?.sections?.find((candidate) => candidate.id === location.sectionID);
    if (!section) {
        throw new HttpError(404, `Unknown section: ${location.pageID}/${location.sectionID}`);
    }
    return section;
}

function findControlInSection(section: PageSection, controlID: string): ControlSpec {
    const control = section.controls?.find((candidate) => candidate.id === controlID);
    if (!control) {
        throw new HttpError(404, `Unknown control: ${controlID}`);
    }
    return control;
}

function findSetting(control: ControlSpec, settingID: string): ConfigSetting {
    const setting = control.settings?.find((candidate) => candidate.id === settingID);
    if (!setting) {
        throw new HttpError(404, `Unknown setting: ${control.id}.${settingID}`);
    }
    return setting;
}

function describe(location: ManifestLocation) {
    return [location.pageID, location.sectionID, location.controlID, location.settingID].filter(Boolean).join("/");
}
