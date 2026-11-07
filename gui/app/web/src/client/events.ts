import { api } from "./api.js";
import { configValueKey } from "../../../shared/rendering.js";
import { bundlePickerDefaultPath, rememberBundlePickerPath } from "./bundle-picker-memory.js";
import { clamp } from "./dom.js";
import { normalizeColorTheme, normalizeIconSet } from "./icons.js";
import { elements, errorMessage, findControl, resolveText } from "./model.js";
import { checkedOptionsChanged, configSettingChanged, fieldValueChanged, loadConfig, cancelSetup, openBundleWorkspace, persistBundleState, retryActionPrecheck, retryDataSource, runAction, runSetup, saveConfig } from "./operations.js";
import { pathPickerDefaultPath, type PathPickerSpec } from "./path-picker-defaults.js";
import { nextFrame, scheduleRender } from "./rerender.js";
import { state } from "./state.js";
import { dismissUpdatePopover, downloadUpdate, installUpdate, toggleUpdatePopover } from "./tauri-updater.js";
import { appendTerminal, closeTerminalTab, resetTerminalEntries, terminalTabs } from "./terminal.js";
import { bindTooltipEvents } from "./tooltips.js";
import { setupPageID } from "./view.js";
import type { ConfigSetting, ControlSpec, PathPickResponse } from "../../../shared/types.js";
export { bindTooltipEvents } from "./tooltips.js";

/** Reloads the manifest and re-renders, optionally for another locale. */
type Bootstrap = (locale?: string) => Promise<void>;
const app: HTMLElement = (() => {
    const el = document.querySelector<HTMLElement>("#app");
    if (!el) {
        throw new Error("Missing required root element: `#app`");
    }
    return el;
})();
let terminalCopyFeedbackTimer = 0;
let updateOutsideClickBound = false;
let bundleLoadInFlight = false;
let installedGlobalBundleLoadHandler = false;
let installedGlobalAboutHandler = false;
export function bindEvents(bootstrap: Bootstrap) {
    installGlobalBundleLoadHandler(bootstrap);
    installGlobalAboutHandler();
    bindTooltipEvents();
    bindSplitters();
    bindUpdateEvents();
    elements("[data-page-id]").forEach((button) => {
        button.addEventListener("click", async () => {
            state.activePageID = requiredDataset(button, "pageId");
            await persistAndRender();
        });
    });
    elements<HTMLSelectElement>("[data-locale-picker]").forEach((picker) => {
        picker.addEventListener("change", async (event) => {
            const target = event.currentTarget as HTMLSelectElement;
            state.dataSourcePayloads.clear();
            state.dataSourceErrors.clear();
            const useSystemDefault = target.value === "__system__";
            state.localizationCode = useSystemDefault ? "" : target.value;
            state.usingSystemDefaultLocale = useSystemDefault;
            await persistBundleState();
            await bootstrap(useSystemDefault ? undefined : target.value);
        });
    });
    app.querySelector<HTMLSelectElement>("[data-icon-set-picker]")?.addEventListener("change", async (event) => {
        state.iconSet = normalizeIconSet((event.currentTarget as HTMLSelectElement).value);
        await persistAndRender();
    });
    app.querySelector<HTMLSelectElement>("[data-color-theme-picker]")?.addEventListener("change", async (event) => {
        state.colorTheme = normalizeColorTheme((event.currentTarget as HTMLSelectElement).value);
        await persistAndRender();
    });
    app.querySelector<HTMLSelectElement>("[data-web-font-picker]")?.addEventListener("change", async (event) => {
        state.webUIFont = (event.currentTarget as HTMLSelectElement).value === "sfPro" ? "sfPro" : "system";
        await persistAndRender();
    });
    app.querySelector("[data-run-setup]")?.addEventListener("click", async () => {
        await runSetup();
    });
    app.querySelector("[data-cancel-setup]")?.addEventListener("click", () => {
        cancelSetup();
    });
    elements("[data-setup-global-start], [data-setup-prompt-run]").forEach((button) => {
        button.addEventListener("click", async () => {
            state.setupPromptVisible = false;
            state.setupPromptDismissed = true;
            state.activePageID = setupPageID();
            await persistAndRender();
            await runSetup();
        });
    });
    app.querySelector("[data-setup-prompt-dismiss]")?.addEventListener("click", () => {
        state.setupPromptVisible = false;
        state.setupPromptDismissed = true;
        scheduleRender();
    });
    app.querySelector("[data-open-bundle-workspace]")?.addEventListener("click", async () => {
        await openBundleWorkspace();
    });
    app.querySelector("[data-load-bundle]")?.addEventListener("click", async () => {
        await loadBundleFromPicker(bootstrap);
    });
    elements<HTMLInputElement | HTMLSelectElement>("[data-field-id]").forEach((input) => {
        input.addEventListener("change", async () => {
            const control = findControl(requiredDataset(input, "fieldId"));
            await fieldValueChanged(input.dataset.toggle != null ? String((input as HTMLInputElement).checked) : input.value, control);
            clearDataSourcesAndRender();
        });
    });
    elements("[data-path-prompt]").forEach((button) => {
        button.addEventListener("click", async (event) => {
            event.preventDefault();
            event.stopPropagation();
            const id = requiredDataset(button, "pathPrompt");
            const control = findControl(id);
            const value = await chooseLocalPath(control, state.fieldValues[id] ?? "");
            if (value) {
                await fieldValueChanged(value, control);
                clearDataSourcesAndRender();
            }
        });
    });
    elements<HTMLInputElement>("[data-check-group]").forEach((input) => {
        input.addEventListener("change", async () => {
            const groupID = requiredDataset(input, "checkGroup");
            const selected = state.checkedOptions[groupID] ?? new Set<string>();
            state.checkedOptions[groupID] = selected;
            input.checked ? selected.add(input.value) : selected.delete(input.value);
            await checkedOptionsChanged(selected, findControl(groupID));
            clearDataSourcesAndRender();
        });
    });
    elements<HTMLInputElement>("[data-config-path]").forEach((input) => {
        input.addEventListener("change", async () => {
            state.configFilePaths[requiredDataset(input, "configPath")] = input.value;
            await persistBundleState();
        });
    });
    elements<HTMLInputElement | HTMLSelectElement>("[data-config-control][data-config-setting]").forEach((input) => {
        input.addEventListener("change", async () => {
            const control = findControl(requiredDataset(input, "configControl"));
            const setting = findSetting(control, requiredDataset(input, "configSetting"));
            const value = input.dataset.toggle != null ? String((input as HTMLInputElement).checked) : input.value;
            await configSettingChanged(value, setting, control);
            clearDataSourcesAndRender();
        });
    });
    elements("[data-config-path-prompt]").forEach((button) => {
        button.addEventListener("click", async (event) => {
            event.preventDefault();
            event.stopPropagation();
            const [controlID, settingID] = requiredDataset(button, "configPathPrompt").split(":");
            const control = findControl(controlID ?? "");
            const setting = findSetting(control, settingID ?? "");
            const key = configValueKey(control, setting);
            const value = await chooseLocalPath(setting, state.configValues[key] ?? "");
            if (value) {
                await configSettingChanged(value, setting, control);
                clearDataSourcesAndRender();
            }
        });
    });
    elements("[data-load-config]").forEach((button) => {
        button.addEventListener("click", async () => {
            await loadConfig(findControl(requiredDataset(button, "loadConfig")));
            scheduleRender();
        });
    });
    elements("[data-save-config]").forEach((button) => {
        button.addEventListener("click", async () => {
            await saveConfig(findControl(requiredDataset(button, "saveConfig")), true);
            scheduleRender();
        });
    });
    elements("[data-action-id]").forEach((button) => {
        button.addEventListener("click", async () => {
            let action;
            let context;
            let reference;
            try {
                action = JSON.parse(button.dataset.action ?? "");
                context = JSON.parse(button.dataset.actionContext ?? "");
                reference = JSON.parse(button.dataset.actionRef ?? "");
            }
            catch (error) {
                const message = error instanceof Error ? error.message : String(error);
                console.error("Failed to parse action data:", error);
                appendTerminal("error", state.labels.terminalProcessErrorTitle, message);
                scheduleRender();
                return;
            }
            await runAction(action, context, reference);
        });
    });
    elements("[data-terminal-tab]").forEach((button) => {
        button.addEventListener("click", () => {
            state.activeTerminalIndex = Number(button.dataset.terminalTab);
            scheduleRender();
        });
    });
    elements("[data-terminal-tab-close]").forEach((button) => {
        button.addEventListener("click", () => {
            closeTerminalTab(Number(button.dataset.terminalTabClose));
            scheduleRender();
        });
    });
    app.querySelector("[data-terminal-copy]")?.addEventListener("click", async () => {
        const entry = terminalTabs()[state.activeTerminalIndex] ?? terminalTabs()[0];
        await copyText(entry?.body ?? "");
        state.terminalCopyFeedback = true;
        window.clearTimeout(terminalCopyFeedbackTimer);
        terminalCopyFeedbackTimer = window.setTimeout(() => {
            state.terminalCopyFeedback = false;
            scheduleRender();
        }, 1600);
        scheduleRender();
    });
    app.querySelector("[data-terminal-toggle]")?.addEventListener("click", () => {
        state.isTerminalVisible = !state.isTerminalVisible;
        scheduleRender();
    });
    app.querySelector("[data-sidebar-toggle]")?.addEventListener("click", () => {
        state.isSidebarVisible = !state.isSidebarVisible;
        localStorage.setItem("guiForCLI.sidebarVisible", state.isSidebarVisible ? "true" : "false");
        scheduleRender();
    });
    elements("[data-retry-source]").forEach((button) => {
        button.addEventListener("click", () => {
            retryDataSource(requiredDataset(button, "retrySource"));
        });
    });
    elements("[data-retry-precheck]").forEach((button) => {
        button.addEventListener("click", () => {
            retryActionPrecheck(requiredDataset(button, "retryPrecheck"));
        });
    });
    app.querySelector("[data-confirm-cancel]")?.addEventListener("click", () => {
        state.pendingConfirmation = null;
        scheduleRender();
    });
    app.querySelector<HTMLInputElement>("[data-confirm-input]")?.addEventListener("input", (event) => {
        const target = event.currentTarget as HTMLInputElement;
        const pending = state.pendingConfirmation;
        if (!pending?.action.confirm) {
            return;
        }
        pending.input = target.value;
        const requiredText = resolveText(pending.action.confirm.requiredText ?? "", pending.context);
        const button = app.querySelector<HTMLButtonElement>("[data-confirm-run]");
        if (button) {
            button.disabled = Boolean(requiredText && target.value !== requiredText);
        }
    });
    app.querySelector("[data-confirm-run]")?.addEventListener("click", async () => {
        const pending = state.pendingConfirmation;
        if (!pending) {
            return;
        }
        state.pendingConfirmation = null;
        const { confirm: _confirm, ...action } = pending.action;
        await runAction(action, pending.context, pending.reference);
    });
    app.querySelector("[data-about-close]")?.addEventListener("click", closeAboutDialog);
    app.querySelector("[data-about-backdrop]")?.addEventListener("click", (event) => {
        if (event.target === event.currentTarget) {
            closeAboutDialog();
        }
    });
    app.querySelector("[data-about-dialog]")?.addEventListener("keydown", (event) => {
        const keyboardEvent = event as KeyboardEvent;
        if (keyboardEvent.key === "Escape") {
            keyboardEvent.preventDefault();
            closeAboutDialog();
        }
    });
}

function bindUpdateEvents() {
    app.querySelector("[data-update-toggle]")?.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        const wasVisible = state.update.popoverVisible;
        toggleUpdatePopover();
        if (!wasVisible) {
            nextFrame(focusUpdatePopover);
        }
    });
    const updatePopover = app.querySelector("[data-update-popover]") as HTMLElement | null;
    updatePopover?.addEventListener("click", (event) => {
        event.stopPropagation();
    });
    updatePopover?.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            dismissUpdatePopover();
            nextFrame(() => (app.querySelector("[data-update-toggle]") as HTMLElement | null)?.focus());
        }
    });
    app.querySelector("[data-update-download]")?.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        await downloadUpdate("user");
    });
    app.querySelector("[data-update-install]")?.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        await installUpdate();
    });
    if (!updateOutsideClickBound) {
        updateOutsideClickBound = true;
        document.addEventListener("click", (event) => {
            const target = event.target;
            if (target instanceof Element && target.closest("[data-update-surface]")) {
                return;
            }
            dismissUpdatePopover();
        });
    }
}

function focusUpdatePopover() {
    (app.querySelector("[data-update-popover]") as HTMLElement | null)?.focus();
}

function installGlobalBundleLoadHandler(bootstrap: Bootstrap) {
    if (installedGlobalBundleLoadHandler) {
        return;
    }
    installedGlobalBundleLoadHandler = true;
    window.addEventListener("gui-for-cli-load-bundle", () => {
        void loadBundleFromPicker(bootstrap);
    });
}

function installGlobalAboutHandler() {
    if (installedGlobalAboutHandler) {
        return;
    }
    installedGlobalAboutHandler = true;
    window.addEventListener("gui-for-cli-show-about", showAboutDialog);
}

function showAboutDialog() {
    state.aboutDialogVisible = true;
    scheduleRender();
    requestAnimationFrame(() => {
        (app.querySelector("[data-about-dialog]") as HTMLElement | null)?.focus();
    });
}

function closeAboutDialog() {
    state.aboutDialogVisible = false;
    scheduleRender();
}

export async function loadBundleFromPicker(bootstrap: Bootstrap) {
    if (bundleLoadInFlight) {
        return;
    }
    bundleLoadInFlight = true;
    try {
        const selectedPath = await chooseLocalPath(
            {
                id: "bundle_root",
                label: "Bundle",
                pathKind: "bundle",
                defaultDirectory: bundlePickerDefaultPath(state.sourceRootPath || state.bundleRootPath || ""),
            },
            "",
        );
        if (!selectedPath) {
            return;
        }
        await api<void>("/api/bundle/load", {
            method: "POST",
            body: { path: selectedPath },
        });
        rememberBundlePickerPath(selectedPath);
        resetBundleClientState();
        await bootstrap();
        appendTerminal("config", `[bundle] Loaded ${state.manifest?.displayName ?? "bundle"}`, state.sourceRootPath);
        scheduleRender();
    }
    catch (error) {
        appendTerminal("error", "Could not load bundle", errorMessage(error));
        scheduleRender();
    }
    finally {
        bundleLoadInFlight = false;
    }
}

function resetBundleClientState() {
    state.activePageID = "";
    state.dataSourcePayloads.clear();
    state.dataSourceErrors.clear();
    state.loadingDataSources.clear();
    state.fileStateValues.clear();
    state.loadingFileStates.clear();
    state.actionPrechecks.clear();
    state.actionPrecheckErrors.clear();
    state.loadingActionPrechecks.clear();
    state.setupRun = null;
    state.setupPreflight = null;
    state.setupPreflightError = "";
    state.setupPreflightKey = "";
    state.loadingSetupPreflight = false;
    state.setupPromptVisible = false;
    state.setupPromptDismissed = false;
    state.aboutDialogVisible = false;
    state.pendingConfirmation = null;
    resetTerminalEntries();
}

async function persistAndRender(options = {}) {
    await persistBundleState(options);
    scheduleRender();
}

function clearDataSourcesAndRender() {
    state.dataSourcePayloads.clear();
    state.dataSourceErrors.clear();
    scheduleRender();
}
async function chooseLocalPath(spec: PathPickerSpec, currentValue: unknown) {
    try {
        const result = await api<PathPickResponse>("/api/path/pick", {
            method: "POST",
            body: {
                kind: pathPickerKind(spec),
                title: pathPickerTitle(spec),
                defaultPath: pathPickerDefaultPath(spec, currentValue, state),
            },
        });
        return result.cancelled ? null : result.path ?? null;
    }
    catch (error) {
        appendTerminal("error", pathPickerTitle(spec), errorMessage(error));
        scheduleRender();
        return null;
    }
}
async function copyText(text: string) {
    if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        return;
    }
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.append(textarea);
    textarea.select();
    try {
        document.execCommand("copy");
    }
    finally {
        textarea.remove();
    }
}
function requiredDataset(element: HTMLElement, key: string): string {
    const value = element.dataset[key];
    if (!value) {
        throw new Error(`Missing required data-${key.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)} attribute.`);
    }
    return value;
}
function findSetting(control: ControlSpec, settingID: string): ConfigSetting {
    const setting = control.settings?.find((candidate) => candidate.id === settingID);
    if (!setting) {
        throw new Error(`Unknown setting: ${control.id}.${settingID}`);
    }
    return setting;
}
function pathPickerTitle(spec: PathPickerSpec) {
    return spec?.label ? `${state.labels.chooseButtonTitle} ${spec.label}` : state.labels.chooseButtonTitle;
}
function pathPickerKind(spec: PathPickerSpec) {
    const explicitKind = String(spec?.pathType ?? spec?.pathKind ?? spec?.pathMode ?? "").toLowerCase();
    if (explicitKind === "directory" || explicitKind === "folder") {
        return "directory";
    }
    if (explicitKind === "file" || explicitKind === "bundle") {
        return explicitKind;
    }
    const id = String(spec?.id ?? "").toLowerCase();
    const key = String(spec?.key ?? "").toLowerCase();
    const label = String(spec?.label ?? "").toLowerCase();
    const tooltip = String(spec?.tooltip ?? "").toLowerCase();
    const searchable = `${id} ${key} ${label} ${tooltip}`;
    if (id === "ref_path" || key === "reference_library") {
        return "directory";
    }
    if (/(^|[_\s-])(out|output)[_\s-]*(dir|directory)($|[_\s-])/.test(searchable) ||
        /(^|[_\s-])(dir|directory|folder|library|cache)($|[_\s-])/.test(searchable)) {
        return "directory";
    }
    return "file";
}
export function bindSplitters() {
    if (!state.isSidebarVisible) {
        return;
    }
    app.querySelector("[data-sidebar-resizer]")?.addEventListener("pointerdown", (event) => {
        const pointerEvent = event as PointerEvent;
        event.preventDefault();
        const startX = pointerEvent.clientX;
        const startWidth = state.sidebarWidth;
        document.body.classList.add("resizing-sidebar");
        const move = (moveEvent: PointerEvent) => {
            state.sidebarWidth = clamp(startWidth + moveEvent.clientX - startX, 160, 420);
            app.style.setProperty("--sidebar-width", `${state.sidebarWidth}px`);
        };
        const up = () => {
            localStorage.setItem("guiForCLI.sidebarWidth", String(Math.round(state.sidebarWidth)));
            document.body.classList.remove("resizing-sidebar");
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", up);
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up, { once: true });
    });
    app.querySelector("[data-terminal-resizer]")?.addEventListener("pointerdown", (event) => {
        const pointerEvent = event as PointerEvent;
        event.preventDefault();
        const startY = pointerEvent.clientY;
        const startHeight = state.terminalHeight;
        document.body.classList.add("resizing-terminal");
        const move = (moveEvent: PointerEvent) => {
            state.terminalHeight = clamp(startHeight - (moveEvent.clientY - startY), 96, Math.max(96, window.innerHeight - 260));
            app.style.setProperty("--terminal-height", `${state.terminalHeight}px`);
        };
        const up = () => {
            localStorage.setItem("guiForCLI.terminalHeight", String(Math.round(state.terminalHeight)));
            document.body.classList.remove("resizing-terminal");
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", up);
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up, { once: true });
    });
}
