import { escapeAttribute, escapeHTML } from "../dom.js";
import { state } from "../state.js";
import type { BundleManifest, SetupStep as BundleSetupStep } from "../../../../shared/types.js";

export const githubURL = "https://github.com/WGSExtract/wgsextract-cli";

type AboutSetupStep = BundleSetupStep & {
    toolName?: unknown;
    toolVersion?: unknown;
};

export function renderAboutDialog() {
    const appName = String(state.applicationName || "GUI for CLI").trim();
    const versions = aboutVersionRows({
        guiForCliVersion: state.appVersion || state.applicationVersion,
        manifest: state.manifest,
    });
    return `
      <div class="modal-backdrop" data-about-backdrop role="presentation">
        <section class="confirmation-modal about-modal" data-about-dialog role="dialog" aria-modal="true" aria-labelledby="about-title" tabindex="-1">
          <h2 id="about-title">About ${escapeHTML(appName)}</h2>
          <p class="about-license">MIT License</p>
          <dl class="about-version-list">
            ${versions
                .map(
                    (row) => `
              <div>
                <dt>${escapeHTML(row.label)}</dt>
                <dd>${escapeHTML(row.value)}</dd>
              </div>`
                )
                .join("")}
          </dl>
          <p class="about-github">GitHub: <a href="${escapeAttribute(githubURL)}" target="_blank" rel="noreferrer" data-about-github>${escapeHTML(githubURL)}</a></p>
          <div class="modal-actions">
            <button type="button" class="action-button primary" data-about-close>OK</button>
          </div>
        </section>
      </div>
    `;
}

export function aboutVersionRows({
    guiForCliVersion,
    manifest,
}: {
    guiForCliVersion?: unknown;
    manifest?: BundleManifest | null;
}) {
    const rows: Array<[string, string]> = [
        ["GUI for CLI version", stringValue(guiForCliVersion)],
        ["Bundle version", stringValue(manifest?.version)],
        ["Tool version", manifestToolVersion(manifest)],
    ];
    return rows.map(([label, value]) => ({
        label,
        value: displayAboutValue(value),
    }));
}

export function manifestToolVersion(manifest?: BundleManifest | null) {
    const step = firstToolVersionStep([
        ...(manifest?.setup?.steps ?? []),
        ...(manifest?.uninstall?.steps ?? []),
    ]);
    if (!step) {
        return "";
    }
    const toolName = stringValue(step.toolName);
    const toolVersion = stringValue(step.toolVersion);
    return toolName ? `${toolName} ${toolVersion}` : toolVersion;
}

function firstToolVersionStep(steps: AboutSetupStep[]) {
    return steps.find((step) => stringValue(step.toolVersion));
}

function displayAboutValue(value: string) {
    return value || "Not specified";
}

function stringValue(value: unknown) {
    return typeof value === "string" ? value.trim() : "";
}
