import type { ValueMap } from "./types.js";

export function serializeFlatToml(values: Record<string, unknown>): string {
    return `${Object.entries(values)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, value]) => `${tomlKey(key)} = ${tomlValue(value)}`)
        .join("\n")}\n`;
}
export function parseFlatToml(text: string): ValueMap {
    const values: ValueMap = Object.create(null);
    for (const rawLine of text.split(/\r?\n/)) {
        const line = rawLine.trim();
        if (!line || line.startsWith("#") || !line.includes("=")) {
            continue;
        }
        const separator = assignmentSeparator(line);
        if (separator < 0) {
            continue;
        }
        const rawKey = line.slice(0, separator).trim();
        const rawValue = stripInlineComment(line.slice(separator + 1).trim());
        const key = rawKey.startsWith('"') ? String(parseTomlValue(rawKey)) : rawKey;
        values[key] = parseTomlValue(rawValue);
    }
    return values;
}
function assignmentSeparator(line: string): number {
    return findUnquotedCharacter(line, "=");
}

function stripInlineComment(value: string): string {
    const comment = findUnquotedCharacter(value, "#");
    return comment < 0 ? value : value.slice(0, comment).trimEnd();
}

function findUnquotedCharacter(value: string, target: string): number {
    let inQuotes = false;
    let escaped = false;
    for (let index = 0; index < value.length; index += 1) {
        const character = value[index];
        if (escaped) {
            escaped = false;
            continue;
        }
        if (character === "\\" && inQuotes) {
            escaped = true;
            continue;
        }
        if (character === '"') {
            inQuotes = !inQuotes;
            continue;
        }
        if (character === target && !inQuotes) {
            return index;
        }
    }
    return -1;
}

function tomlKey(key: string): string {
    return /^[A-Za-z0-9_-]+$/.test(key) ? key : tomlValue(key);
}
function tomlValue(value: unknown): string {
    if (typeof value === "boolean") {
        return String(value);
    }
    if (typeof value === "number") {
        if (!Number.isFinite(value)) {
            throw new Error("TOML numbers must be finite.");
        }
        return String(value);
    }
    return `"${String(value ?? "")
        .replaceAll("\\", "\\\\")
        .replaceAll('"', '\\"')
        .replaceAll("\n", "\\n")}"`;
}
const tomlEscapes: Record<string, string> = { n: "\n", r: "\r", t: "\t", '"': '"', "\\": "\\" };

function parseTomlValue(value: string): string | number | boolean {
    if (!value.startsWith('"') || !value.endsWith('"')) {
        if (value === "true" || value === "false") {
            return value === "true";
        }
        const numericValue = value.replaceAll("_", "");
        const decimal = /^[+-]?\d(?:_?\d)*(?:\.\d(?:_?\d)*)?(?:[eE][+-]?\d(?:_?\d)*)?$/.test(value);
        const basedInteger = /^0(?:x[0-9a-fA-F](?:_?[0-9a-fA-F])*|o[0-7](?:_?[0-7])*|b[01](?:_?[01])*)$/.test(value);
        if ((decimal || basedInteger)
            && Number.isFinite(Number(numericValue))) {
            return Number(numericValue);
        }
        return value;
    }
    return value
        .slice(1, -1)
        .replace(/\\([nrt"\\])/g, (_, escaped: string) => tomlEscapes[escaped] ?? escaped);
}
