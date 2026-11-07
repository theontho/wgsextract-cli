import type { ActionSpec, BundleManifest, BundlePage, ConfigSetting, ControlOption, ControlSpec, PageSection, RowTagSpec, StringMap } from "./types.js";

export function parseTomlStrings(text: string): StringMap {
    const values: StringMap = {};
    const lines = text.split(/\r?\n/);
    let index = 0;
    while (index < lines.length) {
        const rawLine = lines[index] ?? "";
        const lineNumber = index + 1;
        const line = rawLine.trim();
        index += 1;
        if (!line || line.startsWith("#")) {
            continue;
        }
        if (line.startsWith("[") && line.endsWith("]")) {
            throw new Error(`Invalid localization TOML at line ${lineNumber}: ${rawLine}`);
        }
        const equals = findUnescapedEquals(line);
        if (equals < 0) {
            throw new Error(`Invalid localization TOML at line ${lineNumber}: ${rawLine}`);
        }
        const key = unquoteKey(line.slice(0, equals).trim());
        let rawValue = line.slice(equals + 1).trimStart();
        if (rawValue.startsWith('"""')) {
            rawValue = rawValue.slice(3);
            const collected: string[] = [];
            const sameLineEnd = rawValue.indexOf('"""');
            if (sameLineEnd >= 0) {
                collected.push(rawValue.slice(0, sameLineEnd));
            }
            else {
                collected.push(rawValue);
                let foundEnd = false;
                while (index < lines.length) {
                    const nextLine = lines[index] ?? "";
                    index += 1;
                    const end = nextLine.indexOf('"""');
                    if (end >= 0) {
                        collected.push(nextLine.slice(0, end));
                        foundEnd = true;
                        break;
                    }
                    collected.push(nextLine);
                }
                if (!foundEnd) {
                    throw new Error(`Unterminated multiline localization string: ${key}`);
                }
            }
            if (collected[0] === "") {
                collected.shift();
            }
            if (collected[collected.length - 1] === "") {
                collected.pop();
            }
            values[key] = collected.join("\n");
            continue;
        }
        if (!rawValue.startsWith('"')) {
            throw new Error(`Invalid localization TOML at line ${lineNumber}: ${rawLine}`);
        }
        const closing = findClosingQuote(rawValue);
        if (closing < 0) {
            throw new Error(`Invalid localization TOML at line ${lineNumber}: ${rawLine}`);
        }
        const trailing = rawValue.slice(closing + 1).trim();
        if (trailing && !trailing.startsWith("#")) {
            throw new Error(`Invalid localization TOML at line ${lineNumber}: ${rawLine}`);
        }
        values[key] = unescapeTomlString(rawValue.slice(1, closing));
    }
    return values;
}
export function parseTomlStringValue(text: string, requestedKey: string): string | undefined {
    for (const rawLine of text.split(/\r?\n/)) {
        const line = rawLine.trim();
        if (!line || line.startsWith("#") || line.startsWith("[") && line.endsWith("]")) {
            continue;
        }
        const equals = findUnescapedEquals(line);
        if (equals < 0) {
            continue;
        }
        const key = unquoteKey(line.slice(0, equals).trim());
        if (key !== requestedKey) {
            continue;
        }
        const rawValue = line.slice(equals + 1).trimStart();
        const booleanMatch = /^(true|false)(?:\s*(?:#.*)?)$/.exec(rawValue);
        if (booleanMatch) {
            return booleanMatch[1];
        }
        if (!rawValue.startsWith('"') || rawValue.startsWith('"""')) {
            return undefined;
        }
        const closing = findClosingQuote(rawValue);
        if (closing < 0) {
            return undefined;
        }
        const trailing = rawValue.slice(closing + 1).trim();
        if (trailing && !trailing.startsWith("#")) {
            return undefined;
        }
        return unescapeTomlString(rawValue.slice(1, closing));
    }
    return undefined;
}
export function mergeTables(...tables: Array<StringMap | undefined | null>): StringMap {
    return Object.assign({}, ...tables.filter(Boolean));
}
export function localizationLabels(table: StringMap = {}) {
    return {
        standardOptionsSectionTitle: table["app.standardOptions.title"] ?? "Standard Options",
        languageSectionTitle: table["language.setting.title"] ?? "Interface Language",
        languagePickerLabel: table["language.setting.label"] ?? "Language",
        languageSearchPlaceholder: table["language.setting.searchPlaceholder"] ?? "Search languages",
        languageSystemDefaultLabel: table["language.setting.systemDefault"] ?? "Use system default",
        languageAITranslatedLabel: table["language.setting.aiTranslated"] ?? "AI translated",
        iconSetPickerLabel: table["app.iconSet.label"] ?? "Icons",
        iconSetSwiftSymbolsLabel: table["app.iconSet.sfSymbols"] ?? "SF Symbols",
        iconSetBootstrapIconsLabel: table["app.iconSet.bootstrapIcons"] ?? "Bootstrap Icons",
        iconSetEmojiLabel: table["app.iconSet.emoji"] ?? "Emoji",
        colorThemePickerLabel: table["app.colorTheme.label"] ?? "Theme",
        colorThemeSystemLabel: table["app.colorTheme.system"] ?? "System",
        colorThemeLightLabel: table["app.colorTheme.light"] ?? "Light",
        colorThemeDarkLabel: table["app.colorTheme.dark"] ?? "Dark",
        webUIFontPickerLabel: table["app.webUIFont.label"] ?? "Web Font",
        webUIFontSystemLabel: table["app.webUIFont.system"] ?? "Current priority",
        webUIFontSFProLabel: table["app.webUIFont.sfPro"] ?? "SF Pro when available",
        layoutDirection: layoutDirection(table["language.layoutDirection"]),
        terminalMainTabTitle: table["app.terminal.mainTab.title"] ?? "Main",
        terminalCommandOutputLabel: table["app.terminal.commandOutput.label"] ?? "Command output",
        terminalShowOutputLabel: table["app.terminal.showOutput.label"] ?? "Show Command Output",
        terminalHideOutputLabel: table["app.terminal.hideOutput.label"] ?? "Hide Command Output",
        sidebarShowLabel: table["app.sidebar.show.label"] ?? "Show Sidebar",
        sidebarHideLabel: table["app.sidebar.hide.label"] ?? "Hide Sidebar",
        openBundleWorkspaceTitle: table["actions.settings.settings-paths.open-bundle-workspace.title"] ?? "Open Bundle Workspace",
        openBundleWorkspaceTooltip: table["actions.settings.settings-paths.open-bundle-workspace.tooltip"] ??
            "Open the writable bundle workspace folder.",
        terminalCopyTextLabel: table["app.terminal.copyText.label"] ?? "Copy terminal text",
        terminalCopiedTextLabel: table["app.terminal.copiedText.label"] ?? "Copied!",
        setupTitle: table["app.setup.status.title"] ?? "Setup",
        setupRunButtonTitle: table["app.setup.runButton.title"] ?? "Run Setup",
        setupRerunButtonTitle: table["app.setup.rerunButton.title"] ?? "Rerun Setup",
        setupPromptBodyFormat: table["app.setup.prompt.bodyFormat"] ??
            "Do you want to run setup? %{app} will probably not work properly without running setup.",
        setupPromptAppNameFallback: table["app.setup.prompt.appNameFallback"] ?? "This app",
        setupInitialInstallSizeFormat: table["app.setup.initialInstallSize.format"] ??
            "Initial setup will install about %{size} GB.",
        setupDiskSpaceCheckingTitle: table["app.setup.diskSpace.checking"] ?? "Checking available disk space...",
        setupDiskSpaceCheckFailedFormat: table["app.setup.diskSpace.checkFailedFormat"] ??
            "Could not check available disk space: %{error}",
        setupRunningTitle: table["app.setup.status.running"] ?? "Running setup...",
        setupNoStepsTitle: table["app.setup.status.none"] ?? "No setup steps are defined for this bundle.",
        setupStatusReadyTitle: table["app.setup.status.ready"] ?? "Review and run this bundle's setup steps.",
        setupStatusOkTitle: table["app.setup.status.ok"] ?? "Setup completed successfully.",
        setupStatusFailedTitle: table["app.setup.status.failed"] ?? "Setup failed. Review command output for details.",
        setupToolLabel: table["app.setup.tool.label"] ?? "Tool",
        setupVersionLabel: table["app.setup.version.label"] ?? "Version",
        setupStepPendingTitle: table["app.setup.step.pending"] ?? "Pending",
        setupStepRunningTitle: table["app.setup.step.running"] ?? "Running",
        setupStepOkTitle: table["app.setup.step.ok"] ?? "OK",
        setupStepWarningTitle: table["app.setup.step.warning"] ?? "Warning",
        setupStepFailedTitle: table["app.setup.step.failed"] ?? "Failed",
        chooseButtonTitle: table["app.pathPicker.chooseButton.title"] ?? "Choose...",
        pathPickerErrorTitle: table["app.pathPicker.error.title"] ?? "Could not choose path",
        settingsFileLabel: table["app.settingsFile.label"] ?? "Settings File",
        loadButtonTitle: table["app.loadButton.title"] ?? "Load",
        saveButtonTitle: table["app.saveButton.title"] ?? "Save",
        actionsColumnTitle: table["app.actionsColumn.title"] ?? "Actions",
        loadingTitle: table["app.loading.title"] ?? "Loading...",
        refreshingTitle: table["app.refreshing.title"] ?? "Refreshing...",
        retryButtonTitle: table["app.retryButton.title"] ?? "Retry",
        loadWebUITitle: table["app.error.loadWebUI.title"] ?? "Could not load Web UI",
        libraryEmptyTitle: table["app.library.empty"] ?? "No library items are defined.",
        actionMissingInputsFormat: table["app.action.missingInputs.format"] ?? "Missing: %{inputs}",
        actionUnavailableTitle: table["app.action.unavailable.title"] ?? "This action is not available.",
        terminalCloseTabLabelFormat: table["app.terminal.closeTab.labelFormat"] ?? "Close %{title}",
        terminalExitCodeTitleFormat: table["app.terminal.exitCode.titleFormat"] ?? "Exit code %{code}",
        terminalExitDetailFormat: table["app.terminal.exitCode.detailFormat"] ?? "%{command} exited with code %{code}.",
        terminalNonzeroExitSummary: table["app.terminal.nonzeroExit.summary"] ??
            "The command exited with a non-zero status. Check the command output for details.",
        terminalProcessErrorTitle: table["app.terminal.processError.title"] ?? "Command failed",
        terminalProcessErrorSummary: table["app.terminal.processError.summary"] ?? "The command could not complete.",
        configLoadedFormat: table["app.config.loaded.format"] ?? "Loaded settings from %{path}",
        configLoadErrorFormat: table["app.config.loadError.format"] ?? "Could not load %{label}: %{error}",
        configSavedFormat: table["app.config.saved.format"] ?? "Saved %{count} setting(s) to %{path}",
        configSaveErrorFormat: table["app.config.saveError.format"] ?? "Could not save %{label}: %{error}",
        actionPrecheckDiskSpaceTitle: table["app.action.precheck.diskSpace.title"] ?? "Not enough free disk space",
        actionPrecheckDiskSpaceMessageFormat: table["app.action.precheck.diskSpace.messageFormat"] ??
            "Need %{required} GB free at %{path}, only %{available} GB available.",
        actionPrecheckDiskSpaceInfoTitle: table["app.action.precheck.diskSpace.infoTitle"] ?? "Disk space estimate",
        actionPrecheckDiskSpaceInfoFormat: table["app.action.precheck.diskSpace.infoFormat"] ??
            "Estimated %{required} GB needed at %{path} (%{available} GB free).",
        libraryStatusLabels: {
            installed: table["library.status.installed"] ?? "installed",
            unindexed: table["library.status.unindexed"] ?? "unindexed",
            incomplete: table["library.status.incomplete"] ?? "incomplete",
            missing: table["library.status.missing"] ?? "missing",
        },
        libraryTagLabels: {
            recommended: table["library.tags.recommended"] ?? "Recommended",
        },
    };
}
export function localizeManifest(rawManifest: BundleManifest, table: StringMap = {}): BundleManifest {
    const manifest = localizeFields(structuredClone(rawManifest), table, ["displayName", "summary"]);
    manifest.setup = manifest.setup ?? { steps: [] };
    manifest.setup.steps = (manifest.setup.steps ?? []).map((step) => localizeFields(step, table, ["label"]));
    manifest.exitCodeReference = (manifest.exitCodeReference ?? []).map((entry) => localizeFields(entry, table, ["title", "summary"]));
    manifest.pages = (manifest.pages ?? []).map((page) => localizePage(page, table));
    return manifest;
}
function localizePage(page: BundlePage, table: StringMap): BundlePage {
    return {
        ...localizeFields(page, table, ["title", "summary", "sidebarGroup"]),
        sections: (page.sections ?? []).map((section) => localizeSection(section, table)),
    };
}
function localizeSection(section: PageSection, table: StringMap): PageSection {
    return {
        ...localizeFields(section, table, ["title", "subtitle"]),
        controls: (section.controls ?? []).map((control) => localizeControl(control, table)),
        actions: (section.actions ?? []).map((action) => localizeAction(action, table)),
    };
}
function localizeControl(control: ControlSpec, table: StringMap): ControlSpec {
    const localizedControl: ControlSpec = {
        ...localizeFields(control, table, ["label", "placeholder", "tooltip"]),
        options: (control.options ?? []).map((option) => localizeOption(option, table)),
        columns: (control.columns ?? []).map((column) => localizeFields(column, table, ["title"])),
        rows: (control.rows ?? []).map((row) => localizeRow(row, table)),
        items: (control.items ?? []).map((item) => ({
            ...item,
            values: mapValues(recordValue(item.values) ?? item, (value) => localizedText(String(value), table)),
        })),
        rowActions: (control.rowActions ?? []).map((action) => localizeAction(action, table)),
        settings: (control.settings ?? []).map((setting) => localizeSetting(setting, table)),
    };
    if (control.rowTemplate) {
        localizedControl.rowTemplate = localizeRow(control.rowTemplate, table);
    }
    return localizedControl;
}
function localizeRow<Row extends { tags?: RowTagSpec[] | undefined }>(row: Row, table: StringMap): Row {
    return {
        ...localizeFields(row, table, ["title", "status", "tooltip"]),
        tags: (row.tags ?? []).map((tag) => localizeFields(tag, table, ["title"])),
    };
}
function localizeAction(action: ActionSpec, table: StringMap): ActionSpec {
    const localizedAction = localizeFields(action, table, ["title", "tooltip", "disabledTooltip"]);
    if (action.confirm) {
        localizedAction.confirm = localizeFields(action.confirm, table, ["title", "message", "confirmButtonTitle", "cancelButtonTitle", "requiredText", "prompt"]);
    }
    return localizedAction;
}
function localizeSetting(setting: ConfigSetting, table: StringMap): ConfigSetting {
    return {
        ...localizeFields(setting, table, ["label", "placeholder", "tooltip"]),
        options: (setting.options ?? []).map((option) => localizeOption(option, table)),
    };
}
function localizeOption(option: ControlOption, table: StringMap): ControlOption {
    return localizeFields(option, table, ["title", "group"]);
}
/** Replaces each string field that names a string-table key with its translation. */
function localizeFields<Record extends object>(record: Record, table: StringMap, fields: readonly string[]): Record {
    const next = { ...record } as { [key: string]: unknown };
    for (const field of fields) {
        const value = next[field];
        if (typeof value === "string") {
            next[field] = localizedText(value, table);
        }
    }
    return next as Record;
}
function localizedText(value: string, table: StringMap): string {
    return table[value] ?? value;
}
function recordValue(value: unknown): { [key: string]: unknown } | undefined {
    return value != null && typeof value === "object" && !Array.isArray(value) ? value as { [key: string]: unknown } : undefined;
}
function mapValues(values: { [key: string]: unknown }, transform: (value: unknown) => string): StringMap {
    return Object.fromEntries(Object.entries(values).map(([key, value]) => [key, transform(value)]));
}
function layoutDirection(value: unknown) {
    const normalized = String(value ?? "").trim().toLowerCase();
    return ["rtl", "right-to-left", "righttoleft"].includes(normalized) ? "rtl" : "ltr";
}
function findUnescapedEquals(line: string) {
    let quoted = false;
    let escaped = false;
    for (let i = 0; i < line.length; i += 1) {
        const char = line[i];
        if (escaped) {
            escaped = false;
        }
        else if (char === "\\") {
            escaped = true;
        }
        else if (char === '"') {
            quoted = !quoted;
        }
        else if (char === "=" && !quoted) {
            return i;
        }
    }
    return -1;
}
function findClosingQuote(value: string) {
    let escaped = false;
    for (let i = 1; i < value.length; i += 1) {
        const char = value[i];
        if (escaped) {
            escaped = false;
        }
        else if (char === "\\") {
            escaped = true;
        }
        else if (char === '"') {
            return i;
        }
    }
    return -1;
}
function unquoteKey(key: string) {
    return key.startsWith('"') && key.endsWith('"') ? unescapeTomlString(key.slice(1, -1)) : key;
}
function unescapeTomlString(value: string) {
    return value.replace(/\\([nrt"\\])/g, (_, escaped: string) => {
        switch (escaped) {
            case "n":
                return "\n";
            case "r":
                return "\r";
            case "t":
                return "\t";
            case '"':
                return '"';
            case "\\":
                return "\\";
            default:
                return escaped;
        }
    });
}
