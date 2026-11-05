import type { IconMap } from "./icon-map.js";
export type PrimitiveValue = string | number | boolean | null;
export type StateValue = PrimitiveValue | undefined;
export type StringMap = Record<string, string>;
export type ValueMap = Record<string, StateValue>;
export type LabelTextKey =
    "standardOptionsSectionTitle"
    | "languageSectionTitle"
    | "languagePickerLabel"
    | "languageSearchPlaceholder"
    | "languageSystemDefaultLabel"
    | "languageAITranslatedLabel"
    | "iconSetPickerLabel"
    | "iconSetSwiftSymbolsLabel"
    | "iconSetBootstrapIconsLabel"
    | "iconSetEmojiLabel"
    | "colorThemePickerLabel"
    | "colorThemeSystemLabel"
    | "colorThemeLightLabel"
    | "colorThemeDarkLabel"
    | "webUIFontPickerLabel"
    | "webUIFontSystemLabel"
    | "webUIFontSFProLabel"
    | "layoutDirection"
    | "terminalMainTabTitle"
    | "terminalCommandOutputLabel"
    | "terminalShowOutputLabel"
    | "terminalHideOutputLabel"
    | "sidebarShowLabel"
    | "sidebarHideLabel"
    | "openBundleWorkspaceTitle"
    | "openBundleWorkspaceTooltip"
    | "terminalCopyTextLabel"
    | "terminalCopiedTextLabel"
    | "terminalCancelButtonTitle"
    | "setupTitle"
    | "setupRunButtonTitle"
    | "setupRerunButtonTitle"
    | "setupPromptBodyFormat"
    | "setupPromptAppNameFallback"
    | "setupInitialInstallSizeFormat"
    | "setupDiskSpaceCheckingTitle"
    | "setupDiskSpaceCheckFailedFormat"
    | "setupRunningTitle"
    | "setupNoStepsTitle"
    | "setupStatusReadyTitle"
    | "setupStatusOkTitle"
    | "setupStatusFailedTitle"
    | "setupToolLabel"
    | "setupVersionLabel"
    | "setupStepPendingTitle"
    | "setupStepRunningTitle"
    | "setupStepOkTitle"
    | "setupStepWarningTitle"
    | "setupStepFailedTitle"
    | "chooseButtonTitle"
    | "pathPickerErrorTitle"
    | "settingsFileLabel"
    | "loadButtonTitle"
    | "saveButtonTitle"
    | "actionsColumnTitle"
    | "loadingTitle"
    | "refreshingTitle"
    | "retryButtonTitle"
    | "loadWebUITitle"
    | "libraryEmptyTitle"
    | "actionMissingInputsFormat"
    | "actionUnavailableTitle"
    | "terminalCloseTabLabelFormat"
    | "terminalExitCodeTitleFormat"
    | "terminalExitDetailFormat"
    | "terminalNonzeroExitSummary"
    | "terminalProcessErrorTitle"
    | "terminalProcessErrorSummary"
    | "configLoadedFormat"
    | "configLoadErrorFormat"
    | "configSavedFormat"
    | "configSaveErrorFormat"
    | "actionPrecheckDiskSpaceTitle"
    | "actionPrecheckDiskSpaceMessageFormat"
    | "actionPrecheckDiskSpaceInfoTitle"
    | "actionPrecheckDiskSpaceInfoFormat";
export interface Labels extends Partial<Record<LabelTextKey, string>> {
    libraryStatusLabels?: StringMap;
    libraryTagLabels?: StringMap;
    [key: string]: unknown;
}
export type LooseRecord = Record<string, unknown>;

export interface LocalizedOption {
    code: string;
    displayName: string;
    isAITranslated?: boolean;
}

export interface ControlOption {
    id: string;
    title?: string;
    label?: string;
    selected?: boolean;
    [key: string]: unknown;
}

export interface ConfigSetting {
    id: string;
    key?: string;
    label?: string;
    value?: StateValue;
    options?: ControlOption[];
    dataSource?: LooseRecord;
    kind?: string;
    placeholder?: string;
    tooltip?: string;
    [key: string]: unknown;
}

export interface ConfigBootstrapScriptSpec {
    path: string;
    arguments?: string[];
    environment?: StringMap;
    workingDirectory?: string;
}

export interface ConfigFileSpec {
    path: string;
    bootstrap?: { mode?: string; script?: ConfigBootstrapScriptSpec };
    [key: string]: unknown;
}

export interface RowColumnSpec {
    id: string;
    [key: string]: unknown;
}

export interface RowTagSpec {
    id?: string;
    title?: string;
    [key: string]: unknown;
}

export interface RowSpec {
    id: string;
    title?: string | undefined;
    values?: ValueMap;
    status?: string | undefined;
    tags?: RowTagSpec[];
    tooltip?: string | undefined;
    [key: string]: unknown;
}

export interface RowTemplateSpec {
    id?: string;
    title?: string;
    values?: Record<string, string>;
    status?: string;
    tags?: RowTagSpec[];
    tooltip?: string;
    [key: string]: unknown;
}

export interface ControlSpec {
    id: string;
    kind: string;
    label?: string;
    value?: StateValue;
    options?: ControlOption[];
    settings?: ConfigSetting[];
    configFile?: ConfigFileSpec;
    items?: Array<LooseRecord>;
    rows?: RowSpec[];
    rowTemplate?: RowTemplateSpec;
    columns?: RowColumnSpec[];
    rowActions?: ActionSpec[];
    actions?: ActionSpec[];
    dataSource?: LooseRecord;
    [key: string]: unknown;
}

export interface CommandSpec {
    executable: string;
    arguments?: string[];
    optionalArguments?: string[][];
    [key: string]: unknown;
}

export interface ConditionSpec {
    placeholder: string;
    exists?: boolean;
    equals?: string;
    notEquals?: string;
    in?: string[];
    notIn?: string[];
    lessThan?: string;
    lessThanOrEqual?: string;
    greaterThan?: string;
    greaterThanOrEqual?: string;
    [key: string]: unknown;
}

export interface ConfirmationSpec {
    requiredText?: string;
    title?: string;
    message?: string;
    prompt?: string;
    cancelButtonTitle?: string;
    confirmButtonTitle?: string;
    [key: string]: unknown;
}

export interface ActionSpec {
    id?: string;
    title?: string;
    command: CommandSpec;
    precheck?: LooseRecord;
    confirm?: ConfirmationSpec;
    role?: string;
    iconName?: string;
    textIcon?: string;
    visibleWhen?: ConditionSpec[];
    disabledWhen?: ConditionSpec[];
    disabledTooltip?: string;
    /** Runs the action as a job on the bundle's job server when it is available; `command` is the fallback. */
    serverOperation?: ServerOperationSpec;
    [key: string]: unknown;
}

/** An operation on the bundle's job server: `POST {url}/ops/{operation}` with `parameters` as the JSON body. */
export interface ServerOperationSpec {
    operation: string;
    /** String values are interpolated; those that end up empty are left out. Other JSON values are sent as written. */
    parameters?: LooseRecord;
}

/** A long-running local server, started on first use, that runs actions as cancellable jobs (docs/job-server.md). */
export interface JobServerSpec {
    /** Prints `{"url": ..., "token": ...}` as a line of JSON on stdout once it is listening. */
    command: CommandSpec;
    startupTimeoutSeconds?: number;
}

export interface PageSection {
    id?: string;
    title?: string;
    controls?: ControlSpec[];
    actions?: ActionSpec[];
    dataSource?: LooseRecord;
    visibleWhen?: ConditionSpec[];
    [key: string]: unknown;
}

export interface BundlePage {
    id: string;
    title: string;
    sections?: PageSection[];
    [key: string]: unknown;
}

export interface SetupStep {
    id: string;
    kind: string;
    label?: string;
    value?: string;
    arguments?: string[];
    workingDirectory?: string;
    environment?: StringMap;
    optional?: boolean;
    requiresAdmin?: boolean;
    platforms?: string[];
    [key: string]: unknown;
}

export interface SetupSpec {
    steps?: SetupStep[];
    [key: string]: unknown;
}

export interface BundleManifest {
    id?: string;
    displayName?: string;
    iconPath?: string;
    pages: BundlePage[];
    setup?: SetupSpec;
    uninstall?: SetupSpec;
    defaultLocalizationCode?: string;
    exitCodeReference?: ExitCodeReference[];
    jobServer?: JobServerSpec;
    [key: string]: unknown;
}

export interface CommandContext {
    fieldValues?: ValueMap;
    checkedOptions?: StringMap;
    configValues?: ValueMap;
    rowValues?: ValueMap;
    fileStateValues?: ValueMap;
    placeholderLabels?: Labels;
    bundleRootPath?: string;
    homePath?: string;
    [key: string]: unknown;
}

export interface ProcessRunOptions {
    cwd?: string;
    env?: Record<string, string | undefined>;
    elevatedEnv?: Record<string, string | undefined>;
    requiresAdmin?: boolean;
    signal?: AbortSignal;
    timeoutMs?: number;
    maxOutputBytes?: number;
    maxErrorBytes?: number;
    onStdout?: (text: string) => void | Promise<void>;
    onStderr?: (text: string) => void | Promise<void>;
}

export type ProcessResult = {
    exitCode: number | null;
    signal?: string | null;
    stdout: string;
    stderr: string;
    stdoutTruncated?: boolean;
    stderrTruncated?: boolean;
};

export interface ExitCodeReference {
    code: string | number;
    severity?: string;
    symbol?: string;
    title?: string;
    summary?: string;
    [key: string]: unknown;
}

export type RunProcess = (
    executable: string,
    args: string[],
    options: ProcessRunOptions,
) => Promise<ProcessResult>;

export interface UpdateState {
    supported: boolean;
    checked: boolean;
    popoverVisible: boolean;
    status: string;
    currentVersion: string;
    availableVersion: string;
    updateRid: number | null;
    bytesRid: number | null;
    downloadedBytes: number;
    contentLength: number | null;
    percent: number | null;
    message: string;
    body: string;
}

export interface DataSourcePayload {
    values?: ValueMap;
    options?: ControlOption[];
    rows?: RowSpec[];
    items?: Array<LooseRecord>;
    rowActions?: ActionSpec[];
    actions?: ActionSpec[];
    [key: string]: unknown;
}

export interface PrecheckResult {
    severity?: string;
    message?: string;
    [key: string]: unknown;
}

export interface SetupStepResult {
    id: string;
    status?: string;
    exitCode?: number | null;
    stdout?: string;
    stderr?: string;
    durationMs?: number;
    [key: string]: unknown;
}

export interface SetupRun {
    status?: string;
    results?: SetupStepResult[];
    currentStepID?: string | null;
    currentStepStartedAt?: string | null;
    currentStepElapsedMs?: number;
    error?: string;
    completedAt?: string;
    [key: string]: unknown;
}

export interface BundleStateSnapshot {
    selectedPageID?: string | null;
    iconSet?: unknown;
    colorTheme?: unknown;
    webUIFont?: unknown;
    setupRun?: SetupRun | null;
    [key: string]: unknown;
}

export interface ManifestResponse {
    manifest: BundleManifest;
    devReload?: boolean;
    stateWarning?: string;
    configWarnings?: string[];
    iconMap?: IconMap;
    labels: Labels;
    localizationCode: string;
    localizationOptions: LocalizedOption[];
    usingSystemDefaultLocale?: boolean;
    bundleState?: BundleStateSnapshot | null;
    appVersion?: string;
    bundleRootPath: string;
    sourceRootPath: string;
    fieldValues?: ValueMap;
    checkedOptions?: Record<string, string[]>;
    configValues?: ValueMap;
    configFilePaths?: StringMap;
    autoRunSetup?: boolean;
}

export interface ConfigLoadResponse {
    path: string;
    values: ValueMap;
    warnings: string[];
}

export interface ConfigSaveResponse {
    path: string;
    keyCount: number;
}

export interface PathPickResponse {
    cancelled?: boolean;
    path?: string;
}

export interface FileStateResponse {
    values?: ValueMap;
}

export interface TerminalStatus {
    severity?: string;
    symbol?: string;
    title?: string;
    blurb?: string;
    detail?: string;
}

export interface TerminalEntry {
    id: string;
    kind: string;
    title: string;
    body: string;
    /** Set once old output has been dropped from `body` to cap scrollback. */
    truncated?: boolean;
    command?: string;
    status?: TerminalStatus;
}

/** Identifies a section, control, or config setting in the loaded manifest. */
export interface ManifestLocation {
    pageID: string;
    sectionID: string;
    controlID?: string;
    settingID?: string;
}

/** Identifies an action by where it is declared; the server renders its command from the manifest. */
export interface ActionReference extends ManifestLocation {
    actionID: string;
}

export interface PendingConfirmation {
    action: ActionSpec;
    reference: ActionReference;
    context: CommandContext;
    input: string;
}

export interface WebUIState {
    manifest: BundleManifest | null;
    applicationName: string;
    applicationVersion: string;
    autoUpdate: boolean;
    autoAcceptUpdate: boolean;
    autoUpdateDelaySeconds: number;
    autoUpdateActionDelaySeconds: number;
    update: UpdateState;
    iconMap: IconMap;
    labels: Labels;
    localizationCode: string;
    localizationOptions: LocalizedOption[];
    usingSystemDefaultLocale: boolean;
    iconSet: string;
    colorTheme: string;
    webUIFont: string;
    bundleRootPath: string;
    homePath?: string;
    sourceRootPath: string;
    appVersion: string;
    activePageID: string;
    fieldValues: ValueMap;
    checkedOptions: Record<string, Set<string>>;
    configValues: ValueMap;
    configFilePaths: StringMap;
    dataSourcePayloads: Map<string, DataSourcePayload>;
    dataSourceErrors: Map<string, string>;
    loadingDataSources: Set<string>;
    fileStateValues: Map<string, ValueMap>;
    loadingFileStates: Set<string>;
    actionPrechecks: Map<string, PrecheckResult>;
    actionPrecheckErrors: Map<string, string>;
    loadingActionPrechecks: Set<string>;
    exitCodeReference: Map<number, ExitCodeReference>;
    setupRun: SetupRun | null;
    setupPreflight: PrecheckResult | null;
    setupPreflightError: string;
    setupPreflightKey: string;
    loadingSetupPreflight: boolean;
    setupPromptVisible: boolean;
    setupPromptDismissed: boolean;
    terminalCopyFeedback: boolean;
    terminalEntries: TerminalEntry[];
    activeTerminalIndex: number;
    aboutDialogVisible: boolean;
    isTerminalVisible: boolean;
    isSidebarVisible: boolean;
    pendingConfirmation: PendingConfirmation | null;
    activeTerminalID?: string;
    sidebarWidth: number;
    terminalHeight: number;
    [key: string]: unknown;
}
