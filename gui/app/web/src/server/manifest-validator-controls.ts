import type { ActionSpec, ConfigFileSpec, ConfigSetting, ControlSpec } from "../../../shared/types.js";
import {
    BundleValidationError,
    requireNonEmpty,
    validateConditions,
    validateConfigFilePath,
    validateConfirmation,
    validateDataSource,
    validateRelativePath,
    validateTextIcon,
    validateUniqueIDs,
} from "./manifest-validation.js";

export function validateAction(action: ActionSpec, basePath: string): void {
    requireNonEmpty(action.id, `${basePath}.id`);
    requireNonEmpty(action.title, `${basePath}.title`);
    validateTextIcon(action.textIcon, `${basePath}.textIcon`);
    if (typeof action.command?.executable !== "string" || !action.command.executable.trim()) {
        throw new BundleValidationError(`Action '${action.id}' must define a command executable.`);
    }
    validateConditions(action.visibleWhen, `${basePath}.visibleWhen`);
    validateConditions(action.disabledWhen, `${basePath}.disabledWhen`);
    validateConfirmation(action.confirm, `${basePath}.confirm`);
}

export function validateControl(control: ControlSpec, sectionPath: string): void {
    const basePath = `${sectionPath}.controls.${control.id}`;
    requireNonEmpty(control.id, `${basePath}.id`);
    requireNonEmpty(control.label, `${basePath}.label`);
    validateUniqueIDs(control.options, `${basePath}.options`);
    validateUniqueIDs(control.columns, `${basePath}.columns`);
    validateUniqueIDs(control.rows, `${basePath}.rows`);
    validateUniqueIDs(control.rowActions, `${basePath}.rowActions`);
    validateUniqueIDs(control.settings, `${basePath}.settings`);
    if (control.rowTemplate != null) {
        requireNonEmpty(control.rowTemplate.id, `${basePath}.rowTemplate.id`);
    }
    if (control.dataSource != null) {
        validateDataSource(control.dataSource, `${basePath}.dataSource`);
    }
    if (control.configFile != null) {
        validateConfigFile(control.configFile, basePath);
    }
    for (const column of control.columns ?? []) {
        requireNonEmpty(column.title, `${basePath}.columns.${column.id}.title`);
    }
    for (const rowAction of control.rowActions ?? []) {
        validateAction(rowAction, `${basePath}.rowActions.${rowAction.id}`);
    }
    for (const setting of control.settings ?? []) {
        validateSetting(setting, `${basePath}.settings.${setting.id}`);
    }
}

function validateConfigFile(configFile: ConfigFileSpec, basePath: string): void {
    requireNonEmpty(configFile.path, `${basePath}.configFile.path`);
    validateConfigFilePath(configFile.path, `${basePath}.configFile.path`);
    const script = configFile.bootstrap?.script;
    if (script == null) {
        return;
    }
    requireNonEmpty(script.path, `${basePath}.configFile.bootstrap.script.path`);
    validateRelativePath(script.path, `${basePath}.configFile.bootstrap.script.path`);
    if (script.workingDirectory != null) {
        validateRelativePath(script.workingDirectory, `${basePath}.configFile.bootstrap.script.workingDirectory`);
    }
}

function validateSetting(setting: ConfigSetting, basePath: string): void {
    requireNonEmpty(setting.key, `${basePath}.key`);
    requireNonEmpty(setting.label, `${basePath}.label`);
    validateUniqueIDs(setting.options, `${basePath}.options`);
    if (setting.dataSource != null) {
        validateDataSource(setting.dataSource, `${basePath}.dataSource`);
    }
}
