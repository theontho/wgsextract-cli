import type { UpdateState, WebUIState } from "../../../shared/types.js";

export function createInitialState(): WebUIState {
    const appMetadata = window as Window & {
        GUI_FOR_CLI_APPLICATION_NAME?: string;
        GUI_FOR_CLI_APPLICATION_VERSION?: string;
        GUI_FOR_CLI_AUTO_UPDATE?: boolean;
        GUI_FOR_CLI_AUTO_ACCEPT_UPDATE?: boolean;
        GUI_FOR_CLI_AUTO_UPDATE_DELAY_SECONDS?: number;
        GUI_FOR_CLI_AUTO_UPDATE_ACTION_DELAY_SECONDS?: number;
    };
    return {
        manifest: null,
        applicationName: appMetadata.GUI_FOR_CLI_APPLICATION_NAME ?? "",
        applicationVersion: appMetadata.GUI_FOR_CLI_APPLICATION_VERSION ?? "",
        autoUpdate: Boolean(appMetadata.GUI_FOR_CLI_AUTO_UPDATE),
        autoAcceptUpdate: Boolean(appMetadata.GUI_FOR_CLI_AUTO_ACCEPT_UPDATE),
        autoUpdateDelaySeconds: Number(appMetadata.GUI_FOR_CLI_AUTO_UPDATE_DELAY_SECONDS) || 0,
        autoUpdateActionDelaySeconds: Number(appMetadata.GUI_FOR_CLI_AUTO_UPDATE_ACTION_DELAY_SECONDS) || 0,
        update: createInitialUpdateState(),
        iconMap: {},
        labels: {},
        localizationCode: "",
        localizationOptions: [],
        usingSystemDefaultLocale: true,
        iconSet: "platform",
        colorTheme: "system",
        webUIFont: "system",
        bundleRootPath: "",
        sourceRootPath: "",
        appVersion: "",
        activePageID: "",
        fieldValues: {},
        checkedOptions: {},
        configValues: {},
        configFilePaths: {},
        dataSourcePayloads: new Map(),
        dataSourceErrors: new Map(),
        loadingDataSources: new Set(),
        fileStateValues: new Map(),
        loadingFileStates: new Set(),
        actionPrechecks: new Map(),
        actionPrecheckErrors: new Map(),
        loadingActionPrechecks: new Set(),
        exitCodeReference: new Map(),
        setupRun: null,
        setupPreflight: null,
        setupPreflightError: "",
        setupPreflightKey: "",
        loadingSetupPreflight: false,
        setupPromptVisible: false,
        setupPromptDismissed: false,
        terminalCopyFeedback: false,
        terminalEntries: [],
        activeTerminalIndex: 0,
        aboutDialogVisible: false,
        isTerminalVisible: true,
        isSidebarVisible: localStorage.getItem("guiForCLI.sidebarVisible") !== "false",
        pendingConfirmation: null,
        sidebarWidth: Number(localStorage.getItem("guiForCLI.sidebarWidth")) || 220,
        terminalHeight: Number(localStorage.getItem("guiForCLI.terminalHeight")) ||
            Math.round(window.innerHeight * 0.2),
    };
}

function createInitialUpdateState(): UpdateState {
    return {
        supported: false,
        checked: false,
        popoverVisible: false,
        status: "idle",
        currentVersion: "",
        availableVersion: "",
        updateRid: null,
        bytesRid: null,
        downloadedBytes: 0,
        contentLength: null,
        percent: null,
        message: "",
        body: "",
    };
}

export const state: WebUIState = createInitialState();
