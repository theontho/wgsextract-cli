import { setupPlatformAlias } from "../../../shared/setup-platforms.js";
import type { BundleManifest, BundlePage, PageSection, SetupStep } from "../../../shared/types.js";
import {
    BundleValidationError,
    requireNonEmpty,
    validateDataSource,
    validateRelativePath,
    validateTextIcon,
    validateUniqueIDs,
    validateUniqueValues,
} from "./manifest-validation.js";
import { validateAction, validateControl } from "./manifest-validator-controls.js";

const SCRIPT_STEP_KINDS = new Set(["bundledScript", "setupScript"]);

/** Checks a loaded manifest (with page files already inlined) against the rules in docs/schema. */
export function validateManifest(manifest: BundleManifest): void {
    requireNonEmpty(manifest.id, "id");
    if (manifest.version != null) {
        requireNonEmpty(manifest.version, "version");
    }
    requireNonEmpty(manifest.displayName, "displayName");
    requireNonEmpty(manifest.summary, "summary");
    if (manifest.iconPath != null) {
        validateRelativePath(manifest.iconPath, "iconPath");
    }
    validateTextIcon(manifest.textIcon, "textIcon");

    if (!Array.isArray(manifest.pages) || manifest.pages.length === 0) {
        throw new BundleValidationError("Bundle manifest must define at least one page.");
    }
    validateUniqueIDs(manifest.pages, "pages");
    validateUniqueValues(pageFiles(manifest), "pages");
    validateExitCodeReference(manifest);
    validateSetupSteps(manifest.setup?.steps, "setup.steps");
    validateSetupSteps(manifest.uninstall?.steps, "uninstall.steps");
    for (const page of manifest.pages) {
        validatePage(page);
    }
}

function pageFiles(manifest: BundleManifest): string[] {
    return Array.isArray(manifest.pageFiles) ? manifest.pageFiles.map(String) : [];
}

function validateExitCodeReference(manifest: BundleManifest): void {
    const entries = manifest.exitCodeReference ?? [];
    validateUniqueValues(entries.map((entry) => String(entry.code)), "exitCodeReference");
    for (const entry of entries) {
        requireNonEmpty(entry.title, `exitCodeReference.${entry.code}.title`);
        requireNonEmpty(entry.summary, `exitCodeReference.${entry.code}.summary`);
    }
}

function validateSetupSteps(steps: SetupStep[] | undefined, scope: string): void {
    validateUniqueIDs(steps, scope);
    for (const [index, step] of (steps ?? []).entries()) {
        const base = `${scope}.${step.id}`;
        const indexedBase = `${scope}.${index}`;
        requireNonEmpty(step.id, `${base}.id`);
        requireNonEmpty(step.label, `${base}.label`);
        requireNonEmpty(step.value, `${base}.value`);
        if (SCRIPT_STEP_KINDS.has(step.kind)) {
            validateRelativePath(step.value, `${base}.value`);
        }
        if (step.workingDirectory != null) {
            validateRelativePath(step.workingDirectory, `${base}.workingDirectory`);
        }
        for (const key of ["toolName", "toolVersion"] as const) {
            if (step[key] != null) {
                requireNonEmpty(step[key], `${base}.${key}`);
            }
        }
        if (step.toolVersionFile != null) {
            validateRelativePath(step.toolVersionFile, `${base}.toolVersionFile`);
        }
        if (step.requiresAdmin != null && typeof step.requiresAdmin !== "boolean") {
            throw new BundleValidationError(`Invalid ${indexedBase}.requiresAdmin: expected a boolean.`);
        }
        validateSetupPlatforms(step, indexedBase);
    }
}

function validateSetupPlatforms(step: SetupStep, stepPath: string): void {
    if (step.platforms == null) {
        return;
    }
    if (!Array.isArray(step.platforms)) {
        throw new BundleValidationError(`Invalid ${stepPath}.platforms: expected an array.`);
    }
    step.platforms.forEach((platform, index) => {
        if (setupPlatformAlias(String(platform)) == null) {
            throw new BundleValidationError(`Unsupported setup platform at ${stepPath}.platforms.${index}: ${platform}`);
        }
    });
}

function validatePage(page: BundlePage): void {
    const base = `pages.${page.id}`;
    requireNonEmpty(page.id, `${base}.id`);
    requireNonEmpty(page.title, `${base}.title`);
    requireNonEmpty(page.summary, `${base}.summary`);
    validateTextIcon(page.textIcon, `${base}.textIcon`);
    if (!Array.isArray(page.sections) || page.sections.length === 0) {
        throw new BundleValidationError(`Page '${page.id}' must define at least one section.`);
    }
    validateUniqueIDs(page.sections, `${base}.sections`);
    for (const section of page.sections) {
        validateSection(section, page.id);
    }
}

function validateSection(section: PageSection, pageID: string): void {
    const base = `pages.${pageID}.sections.${section.id}`;
    requireNonEmpty(section.id, `${base}.id`);
    validateTextIcon(section.textIcon, `${base}.textIcon`);
    if (section.dataSource != null) {
        validateDataSource(section.dataSource, `${base}.dataSource`);
    }
    validateUniqueIDs(section.controls, `${base}.controls`);
    validateUniqueIDs(section.actions, `${base}.actions`);
    for (const control of section.controls ?? []) {
        validateControl(control, base);
    }
    for (const action of section.actions ?? []) {
        validateAction(action, `${base}.actions.${action.id}`);
    }
}
