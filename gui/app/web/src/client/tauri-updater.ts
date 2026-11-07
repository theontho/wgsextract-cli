import { scheduleRender } from "./rerender.js";
import { state } from "./state.js";

type TauriInvoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;
type TauriListen = <T>(
    event: string,
    handler: (event: { payload: T }) => void
) => Promise<() => void>;

type TauriGlobal = Window & {
    __TAURI__?: {
        core?: { invoke?: TauriInvoke };
        event?: { listen?: TauriListen };
    };
};

type UpdateCheckResponse = {
    currentVersion: string;
    availableVersion?: string | null;
    updateRid?: number | null;
    body?: string | null;
};

type UpdateDownloadResponse = {
    bytesRid: number;
};

type UpdateProgressEvent = {
    event: string;
    version: string;
    downloadedBytes: number;
    contentLength?: number | null;
    percent?: number | null;
};

let initialized = false;
let checkPromise: Promise<void> | null = null;

export function initializeTauriUpdater() {
    if (initialized) {
        return;
    }
    initialized = true;
    void initializeTauriUpdaterWhenReady();
}

async function initializeTauriUpdaterWhenReady() {
    const tauri = await waitForTauriAPI();
    state.update.supported = Boolean(tauri);
    if (!tauri) {
        return;
    }
    void tauri.event.listen<UpdateProgressEvent>("gfc-update-progress", ({ payload }) => {
        applyProgressEvent(payload);
        scheduleRender();
    });
    const delay = Math.max(0, Number(state.autoUpdateDelaySeconds) || 0);
    window.setTimeout(() => {
        void checkForUpdates({
            revealOnAvailable: Boolean(state.autoUpdate),
            autoDownloadAndInstall: Boolean(state.autoUpdate && state.autoAcceptUpdate),
        });
    }, delay * 1000);
}

export function isUpdateButtonVisible() {
    const update = state.update;
    return update.supported && ["checking", "available", "downloading", "downloaded", "installing", "error"].includes(update.status);
}

export function toggleUpdatePopover() {
    state.update.popoverVisible = !state.update.popoverVisible;
    scheduleRender();
}

export function dismissUpdatePopover() {
    if (!state.update.popoverVisible) {
        return;
    }
    state.update.popoverVisible = false;
    scheduleRender();
}

export async function checkForUpdates(options: { revealOnAvailable?: boolean; autoDownloadAndInstall?: boolean } = {}) {
    if (checkPromise) {
        return checkPromise;
    }
    checkPromise = runUpdateCheck(options).finally(() => {
        checkPromise = null;
    });
    return checkPromise;
}

async function runUpdateCheck(options: { revealOnAvailable?: boolean; autoDownloadAndInstall?: boolean }) {
    const tauri = tauriAPI();
    if (!tauri) {
        state.update.supported = false;
        return;
    }
    state.update.supported = true;
    state.update.checked = true;
    state.update.status = "checking";
    state.update.message = "Checking for updates...";
    scheduleRender();
    try {
        const priorUpdateRid = state.update.updateRid ?? null;
        const priorBytesRid = state.update.bytesRid ?? null;
        const result = await tauri.core.invoke<UpdateCheckResponse>("gfc_update_check", {
            priorUpdateRid,
            priorBytesRid,
        });
        state.update.currentVersion = result.currentVersion || state.applicationVersion || "";
        state.update.availableVersion = result.availableVersion || "";
        state.update.updateRid = result.updateRid ?? null;
        state.update.body = result.body || "";
        state.update.bytesRid = null;
        state.update.downloadedBytes = 0;
        state.update.contentLength = null;
        state.update.percent = null;
        if (result.availableVersion && result.updateRid != null) {
            state.update.status = "available";
            state.update.message = `Version ${result.availableVersion} is ready to download.`;
            state.update.popoverVisible = Boolean(options.revealOnAvailable);
            scheduleRender();
            if (options.autoDownloadAndInstall) {
                await waitForAutoActionDelay();
                await downloadUpdate("auto");
                await waitForAutoActionDelay();
                await installUpdate();
            }
            return;
        }
        state.update.status = "none";
        state.update.message = "You are already running the latest version.";
        state.update.popoverVisible = false;
    } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (message.startsWith("This build is not configured for updates")) {
            state.update.supported = false;
            state.update.status = "none";
            state.update.message = "";
            state.update.popoverVisible = false;
            console.info("Updater disabled:", message);
            return;
        }
        state.update.status = "error";
        state.update.message = message;
        state.update.popoverVisible = Boolean(options.revealOnAvailable);
        console.error("Update check failed:", error);
    } finally {
        scheduleRender();
    }
}

export async function downloadUpdate(acceptedBy = "user") {
    const tauri = tauriAPI();
    if (!tauri || state.update.updateRid == null || state.update.status === "downloading") {
        return;
    }
    // If a previous download already completed, skip re-downloading to avoid
    // orphaning the existing downloaded bytes resource.
    if (state.update.bytesRid != null) {
        return;
    }
    state.update.status = "downloading";
    state.update.message = "Downloading update...";
    state.update.percent = state.update.percent ?? 0;
    state.update.popoverVisible = true;
    scheduleRender();
    try {
        const result = await tauri.core.invoke<UpdateDownloadResponse>("gfc_update_download", {
            updateRid: state.update.updateRid,
            acceptedBy,
        });
        state.update.bytesRid = result.bytesRid;
        state.update.status = "downloaded";
        state.update.percent = 100;
        state.update.message = "Download complete. Ready to install.";
    } catch (error) {
        state.update.status = "error";
        state.update.message = error instanceof Error ? error.message : String(error);
        console.error("Update download failed:", error);
    } finally {
        scheduleRender();
    }
}

export async function installUpdate() {
    const tauri = tauriAPI();
    if (!tauri || state.update.updateRid == null || state.update.bytesRid == null) {
        return;
    }
    state.update.status = "installing";
    state.update.message = "Starting installer. The app may close while the update is installed.";
    state.update.popoverVisible = true;
    scheduleRender();
    try {
        await tauri.core.invoke("gfc_update_install", {
            updateRid: state.update.updateRid,
            bytesRid: state.update.bytesRid,
        });
    } catch (error) {
        state.update.status = "error";
        state.update.message = error instanceof Error ? error.message : String(error);
        console.error("Update install failed:", error);
        scheduleRender();
    }
}

function applyProgressEvent(event: UpdateProgressEvent) {
    if (event.version && !state.update.availableVersion) {
        state.update.availableVersion = event.version;
    }
    if (typeof event.downloadedBytes === "number" && event.downloadedBytes > 0) {
        state.update.downloadedBytes = event.downloadedBytes;
    }
    state.update.contentLength = event.contentLength ?? state.update.contentLength;
    if (typeof event.percent === "number") {
        state.update.percent = Math.max(0, Math.min(100, event.percent));
    }
    if (event.event === "finished") {
        state.update.percent = 100;
        state.update.message = "Download complete. Ready to install.";
    } else if (state.update.status === "downloading") {
        state.update.message = "Downloading update...";
    }
}

function tauriAPI() {
    const tauri = (window as TauriGlobal).__TAURI__;
    if (typeof tauri?.core?.invoke !== "function" || typeof tauri?.event?.listen !== "function") {
        return null;
    }
    return {
        core: { invoke: tauri.core.invoke },
        event: { listen: tauri.event.listen },
    };
}

async function waitForTauriAPI() {
    const deadline = Date.now() + 10_000;
    let tauri = tauriAPI();
    while (!tauri && Date.now() < deadline) {
        await new Promise((resolve) => window.setTimeout(resolve, 100));
        tauri = tauriAPI();
    }
    return tauri;
}

function waitForAutoActionDelay() {
    const delay = Math.max(0, Number(state.autoUpdateActionDelaySeconds) || 0);
    return new Promise<void>((resolve) => window.setTimeout(resolve, delay * 1000));
}
