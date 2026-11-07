import { configEditorControls, initialCheckedOptions, initialConfigValues, initialFieldValues } from "../../../shared/rendering.js";
import { api } from "./api.js";
import { clamp, escapeAttribute, escapeHTML } from "./dom.js";
import { normalizeColorTheme, normalizeIconSet } from "./icons.js";
import { errorMessage } from "./model.js";
import { effectiveWebUIFont, shouldRenderInPageBundleLoader } from "./platform.js";
import { setRender } from "./rerender.js";
import { captureFocus, restoreFocus } from "./focus-state.js";
import { captureScrollState, restoreScrollState, type ScrollSnapshot } from "./scroll-state.js";
import { runSetup } from "./operations-setup.js";
import { state } from "./state.js";
import { initializeTauriUpdater } from "./tauri-updater.js";
import { appendTerminal, discardPendingTerminalOutput, ensureMainTerminal, renderTerminalPane, terminalTabs, terminalToggleTitle } from "./terminal.js";
import { renderUpdatePopover } from "./view/update.js";
import { renderBundleHeader, renderConfirmationDialog, renderNavigation, renderPage, renderSetupGlobalStatusBar, renderSetupPromptDialog, setupHasNeverRun, setupNeedsAttention, setupPageID } from "./view.js";
import { renderAboutDialog } from "./view/about.js";
import type { BundleManifest, ManifestResponse } from "../../../shared/types.js";
const app: HTMLElement = (() => {
    const el = document.querySelector<HTMLElement>("#app");
    if (!el) {
        throw new Error("Missing required root element: `#app`");
    }
    return el;
})();
let bindEventsModulePromise: Promise<typeof import("./events.js")> | null = null;
let renderSerial = 0;
let autoRunSetupTriggered = false;
setRender(render);
await bootstrap();
async function bootstrap(locale?: string) {
    try {
        const bundle = await api<ManifestResponse>(`/api/manifest${locale ? `?locale=${encodeURIComponent(locale)}` : ""}`);
        state.manifest = bundle.manifest;
        state.iconMap = bundle.iconMap ?? {};
        state.labels = bundle.labels;
        state.localizationCode = bundle.localizationCode;
        state.localizationOptions = bundle.localizationOptions;
        state.usingSystemDefaultLocale = Boolean(bundle.usingSystemDefaultLocale);
        state.iconSet = normalizeIconSet(bundle.bundleState?.iconSet);
        state.colorTheme = normalizeColorTheme(bundle.bundleState?.colorTheme);
        state.webUIFont = bundle.bundleState?.webUIFont === "sfPro" ? "sfPro" : "system";
        state.setupRun = bundle.bundleState?.setupRun ?? null;
        state.appVersion = bundle.appVersion ?? "";
        state.workspaceLocation = bundle.workspaceLocation ?? null;
        state.exitCodeReference = new Map((bundle.manifest.exitCodeReference ?? []).map((entry) => [Number(entry.code), entry]));
        state.bundleRootPath = bundle.bundleRootPath;
        state.sourceRootPath = bundle.sourceRootPath;
        ensureMainTerminal();
        if (bundle.stateWarning) {
            appendTerminal("error", "Saved state was reset", bundle.stateWarning);
        }
        if (bundle.configWarnings?.length) {
            appendTerminal("config", state.labels.settingsFileLabel, bundle.configWarnings.join("\n"));
        }
        state.activePageID = validPageID(bundle.bundleState?.selectedPageID, bundle.manifest) ?? state.activePageID ?? bundle.manifest.pages[0]?.id;
        state.fieldValues = bundle.fieldValues ?? initialFieldValues(bundle.manifest);
        state.checkedOptions = Object.fromEntries(Object.entries(bundle.checkedOptions ?? {}).map(([key, value]) => [
            key,
            new Set(Array.isArray(value) ? value : []),
        ]));
        for (const [key, value] of Object.entries(initialCheckedOptions(bundle.manifest))) {
            state.checkedOptions[key] ??= value as Set<string>;
        }
        state.configValues = bundle.configValues ?? initialConfigValues(bundle.manifest);
        state.configFilePaths =
            bundle.configFilePaths ??
                Object.fromEntries(configEditorControls(bundle.manifest)
                    .flatMap((control) => control.configFile ? [[control.id, control.configFile.path]] : []));
        const shouldAutoRunSetup = Boolean(bundle.autoRunSetup) && setupHasNeverRun() && !autoRunSetupTriggered;
        if (shouldAutoRunSetup) {
            state.setupPromptVisible = false;
            state.setupPromptDismissed = true;
            state.activePageID = setupPageID();
        }
        else {
            state.setupPromptVisible = setupHasNeverRun() && !state.setupPromptDismissed;
        }
        render();
        initializeTauriUpdater();
        if (bundle.devReload) {
            installDevReload();
        }
        if (shouldAutoRunSetup) {
            autoRunSetupTriggered = true;
            void runAutoSetup();
        }
    }
    catch (error) {
        renderError(error);
    }
}
async function runAutoSetup() {
    try {
        await runSetup();
    } catch (error) {
        console.error("auto-run setup failed", error);
    }
}
function validPageID(pageID: string | undefined | null, manifest: BundleManifest) {
    return pageID && manifest.pages.some((page) => page.id === pageID) ? pageID : undefined;
}
function render() {
    const serial = ++renderSerial;
    discardPendingTerminalOutput();
    const focus = captureFocus(app);
    const scrollSnapshot = captureRenderScrollSnapshot();
    updateDocumentMetadata();
    document.documentElement.lang = state.localizationCode || "en";
    document.documentElement.dir = state.labels.layoutDirection || "ltr";
    applyDocumentPreferences();
    const manifest = state.manifest;
    if (!manifest) {
        renderError(new Error("Bundle manifest is not loaded."));
        return;
    }
    const activePage = manifest.pages.find((page) => page.id === state.activePageID) ?? manifest.pages[0];
    state.activePageID = activePage?.id ?? "";
    app.classList.toggle("terminal-hidden", !state.isTerminalVisible);
    app.classList.toggle("sidebar-hidden", !state.isSidebarVisible);
    app.classList.toggle("bundle-action-visible", shouldRenderInPageBundleLoader());
    app.classList.toggle("setup-needed", setupNeedsAttention());
    app.style.setProperty("--sidebar-width", `${clamp(state.sidebarWidth, 160, 420)}px`);
    app.style.setProperty("--terminal-height", `${clamp(state.terminalHeight, 96, Math.max(96, window.innerHeight - 260))}px`);
    const activeTerminalID = activeTerminalEntryID();
    app.innerHTML = `
    ${state.isSidebarVisible
        ? `<aside class="sidebar">
      <button type="button" class="sidebar-toggle sidebar-toggle-inside" data-sidebar-toggle title="${escapeAttribute(sidebarToggleTitle())}" aria-label="${escapeAttribute(sidebarToggleTitle())}">◀</button>
      ${renderBundleHeader()}
      <nav class="page-nav" aria-label="Pages">${renderNavigation()}</nav>
    </aside>
    <div class="sidebar-resizer" data-sidebar-resizer role="separator" aria-orientation="vertical" aria-label="Resize sidebar" tabindex="0"></div>`
        : ""}
    <div class="detail-shell">
      ${renderBundleActionBar()}
      ${renderSetupGlobalStatusBar()}
      <main class="page-panel" data-page-panel-id="${escapeAttribute(activePage?.id ?? "")}">${activePage ? renderPage(activePage) : ""}</main>
      ${state.isTerminalVisible ? `<div class="terminal-resizer" data-terminal-resizer role="separator" aria-orientation="horizontal" aria-label="Resize command output" tabindex="0"></div>` : ""}
      ${state.isTerminalVisible ? renderTerminalPane() : ""}
      <button type="button" class="terminal-toggle" data-terminal-toggle title="${escapeAttribute(terminalToggleTitle())}" aria-label="${escapeAttribute(terminalToggleTitle())}">▭</button>
    </div>
    ${state.isSidebarVisible ? "" : `<button type="button" class="sidebar-toggle sidebar-toggle-floating" data-sidebar-toggle title="${escapeAttribute(sidebarToggleTitle())}" aria-label="${escapeAttribute(sidebarToggleTitle())}">▶</button>`}
    ${renderUpdatePopover()}
    ${state.pendingConfirmation ? renderConfirmationDialog() : ""}
    ${renderSetupPromptDialog()}
    ${state.aboutDialogVisible ? renderAboutDialog() : ""}
  `;
    restoreRenderScrollSnapshot(scrollSnapshot, activePage?.id, activeTerminalID);
    restoreFocus(app, focus);
    app.dataset.state = "ready";
    window.dispatchEvent(new Event("gui-for-cli-rendered"));
    bindEventsAfterFirstPaint(serial);
}
function renderBundleActionBar() {
    if (!shouldRenderInPageBundleLoader()) {
        return "";
    }
    return `
      <div class="bundle-action-bar">
        <button type="button" data-load-bundle>Load Bundle...</button>
      </div>
    `;
}
type RenderScrollSnapshot = {
    windowX: number;
    windowY: number;
    page: ScrollSnapshot | null;
    terminal: ScrollSnapshot | null;
};
function captureRenderScrollSnapshot(): RenderScrollSnapshot {
    const pagePanel = app.querySelector<HTMLElement>(".page-panel");
    const terminalLog = app.querySelector<HTMLElement>(".terminal-log");
    return {
        windowX: window.scrollX ?? 0,
        windowY: window.scrollY ?? 0,
        page: captureScrollState(pagePanel, pagePanel?.dataset.pagePanelId),
        terminal: captureScrollState(terminalLog, terminalLog?.dataset.terminalLogId),
    };
}
function restoreRenderScrollSnapshot(snapshot: RenderScrollSnapshot, activePageID: string | undefined, activeTerminalID: string | undefined) {
    const restoredPage = restoreScrollState(app.querySelector<HTMLElement>(".page-panel"), snapshot.page, activePageID);
    restoreScrollState(app.querySelector<HTMLElement>(".terminal-log"), snapshot.terminal, activeTerminalID);
    if (restoredPage && typeof window.scrollTo === "function") {
        window.scrollTo(snapshot.windowX, snapshot.windowY);
    }
}
function activeTerminalEntryID() {
    const tabs = terminalTabs();
    return (tabs[state.activeTerminalIndex] ?? tabs[0])?.id;
}
function sidebarToggleTitle() {
    return state.isSidebarVisible ? state.labels.sidebarHideLabel ?? "Hide Sidebar" : state.labels.sidebarShowLabel ?? "Show Sidebar";
}
function updateDocumentMetadata() {
    document.title = appWindowTitle();
    let favicon = document.querySelector<HTMLLinkElement>("link[data-bundle-favicon]");
    if (!favicon) {
        favicon = document.createElement("link");
        favicon.rel = "icon";
        favicon.sizes = "any";
        favicon.type = "image/x-icon";
        favicon.dataset.bundleFavicon = "";
        document.head.append(favicon);
    }
    const version = encodeURIComponent(state.manifest?.id || "bundle");
    favicon.href = `/favicon.ico?bundle=${version}`;
}
function appWindowTitle() {
    const appName = "GUI for CLI";
    const bundleName = String(state.manifest?.displayName ?? "").trim();
    const appVersion = String(state.appVersion ?? "").trim();
    const baseTitle = bundleName ? `${bundleName} - ${appName}` : appName;
    return appVersion ? `${baseTitle} ${appVersion}` : baseTitle;
}
function applyDocumentPreferences() {
    document.documentElement.dataset.iconSet = state.iconSet;
    if (state.colorTheme === "light" || state.colorTheme === "dark") {
        document.documentElement.dataset.theme = state.colorTheme;
        document.documentElement.style.colorScheme = state.colorTheme;
    }
    else {
        delete document.documentElement.dataset.theme;
        document.documentElement.style.colorScheme = "light dark";
    }
    document.documentElement.dataset.font = effectiveWebUIFont(state.webUIFont);
}
function renderError(error: unknown) {
    const message = errorMessage(error);
    app.dataset.state = "error";
    app.innerHTML = `<main class="loading-screen"><h1>${escapeHTML(state.labels.loadWebUITitle ?? "Could not load Web UI")}</h1><p class="inline-error">${escapeHTML(message)}</p></main>`;
}
let devReloadInstalled = false;
function installDevReload() {
    if (devReloadInstalled || typeof EventSource === "undefined") {
        return;
    }
    devReloadInstalled = true;
    const events = new EventSource("/dev/reload");
    events.addEventListener("reload", () => window.location.reload());
    events.addEventListener("error", () => events.close());
}
function bindEventsAfterFirstPaint(serial: number) {
    requestAnimationFrame(() => {
        if (serial !== renderSerial) {
            return;
        }
        bindEventsModulePromise ??= import("./events.js");
        bindEventsModulePromise
            .then(({ bindEvents }) => bindEvents(bootstrap))
            .catch((error) => renderError(error));
    });
}
