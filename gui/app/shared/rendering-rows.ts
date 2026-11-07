import type { CommandContext, ControlOption, ControlSpec, DataSourcePayload, Labels, LooseRecord, RowSpec, RowTagSpec, ValueMap } from "./types.js";

const PLACEHOLDER_PATTERN = /\{\{([^}]+)\}\}/g;

export function hydrateRows(control: ControlSpec | LooseRecord): RowSpec[] {
    const typedControl = control as ControlSpec;
    if (!(typedControl.items ?? []).length) {
        return typedControl.rows ?? [];
    }
    const template = typedControl.rowTemplate ??
        {
            id: "{{id}}",
            title: "{{name}}",
            values: Object.fromEntries((typedControl.columns ?? []).map((column) => [column.id, `{{${column.id}}}`])),
            status: "{{status}}",
            tags: [],
    };
    return (typedControl.items ?? []).map((item, index) => {
        const values = { ...item, ...objectRecord(item.values) } as ValueMap;
        const fallbackID = nonEmpty(values.id) ?? `row-${index + 1}`;
        const id = nonEmpty(interpolateItem(template.id, values)) ?? fallbackID;
        const title = template.title == null ? undefined : nonEmpty(interpolateItem(template.title, values));
        const status = template.status == null ? undefined : nonEmpty(interpolateItem(template.status, values));
        const tooltip = template.tooltip == null ? undefined : nonEmpty(interpolateItem(template.tooltip, values));
        const templateTags = (template.tags ?? [])
            .map((tag) => ({
            ...tag,
            id: interpolateItem(tag.id, values),
            title: interpolateItem(tag.title, values),
        }))
            .filter((tag) => tag.title.trim());
        return {
            id,
            title: title ?? nonEmpty(item.title),
            values: Object.fromEntries(Object.entries(template.values ?? {}).map(([key, value]) => [key, interpolateItem(value, values)])) as ValueMap,
            status: status ?? nonEmpty(item.status),
            tags: mergeTags(templateTags, arrayOfTags(item.tags)),
            tooltip: tooltip ?? nonEmpty(item.tooltip),
        };
    });
}
function mergeTags(first: RowTagSpec[], second: RowTagSpec[]): RowTagSpec[] {
    const seen = new Set();
    const tags: RowTagSpec[] = [];
    for (const tag of [...first, ...second]) {
        const key = `${tag.id ?? ""}\u0000${tag.title ?? ""}`;
        if (!String(tag.title ?? "").trim() || seen.has(key)) {
            continue;
        }
        seen.add(key);
        tags.push(tag);
    }
    return tags;
}
export function rowContext(baseContext: CommandContext, row: RowSpec): CommandContext {
    const rowValues: ValueMap = { ...(row.values ?? {}), id: row.id, title: row.title ?? row.id };
    if (row.status != null) {
        rowValues.status = row.status;
    }
    return { ...baseContext, rowValues };
}

export function optionTitle(option: ControlOption | RowSpec | LooseRecord, labels: Labels = {}): string {
    const status = option.status ? ` (${labels.libraryStatusLabels?.[String(option.status).toLowerCase()] ?? option.status})` : "";
    return `${option.title ?? option.id}${status}`;
}
export function applyDataSourcePayload(control: ControlSpec | LooseRecord, payload: DataSourcePayload): ControlSpec {
    const next = structuredClone(control) as ControlSpec;
    if (payload.options) {
        next.options = payload.options;
    }
    if (payload.rows) {
        next.rows = payload.rows;
        next.items = [];
    }
    if (payload.items) {
        next.items = payload.items;
    }
    const rowActions = payload.rowActions ?? payload.actions;
    if (rowActions) {
        next.rowActions = rowActions;
    }
    return next;
}

function interpolateItem(value: unknown, values: ValueMap): string {
    return String(value ?? "").replace(PLACEHOLDER_PATTERN, (_, rawPlaceholder) => {
        const raw = rawPlaceholder.trim();
        const placeholder = raw.startsWith("item.") ? raw.slice(5) : raw;
        return String(values[placeholder] ?? "");
    });
}

function nonEmpty(value: unknown): string | undefined {
    const text = value == null ? "" : String(value).trim();
    return text.length ? text : undefined;
}

function objectRecord(value: unknown): LooseRecord {
    return value != null && typeof value === "object" && !Array.isArray(value) ? value as LooseRecord : {};
}

function arrayOfTags(value: unknown): RowTagSpec[] {
    return Array.isArray(value) ? value.filter((item): item is RowTagSpec => item != null && typeof item === "object") : [];
}
