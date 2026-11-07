export type IconMap = Record<string, Record<string, string>>;

export const iconMapSources = {
    sfSymbols: "sf-symbols",
    windows: "windows",
    bootstrap: "bootstrap",
    emoji: "emoji",
} as const;

export function parseIconMapToml(text: string): IconMap {
    const sources: IconMap = {};
    let currentSource: string | undefined;
    const lines = text.split(/\r?\n/);
    for (const [index, rawLine] of lines.entries()) {
        const lineNumber = index + 1;
        const line = rawLine.trim();
        if (!line || line.startsWith("#")) {
            continue;
        }
        if (line.startsWith("[") && line.endsWith("]")) {
            currentSource = line.slice(1, -1).trim();
            if (!currentSource) {
                throw new Error(`Invalid icon map TOML at line ${lineNumber}: ${rawLine}`);
            }
            sources[currentSource] ??= {};
            continue;
        }
        if (!currentSource) {
            throw new Error(`Invalid icon map TOML at line ${lineNumber}: ${rawLine}`);
        }
        const equals = findUnescapedEquals(line);
        if (equals < 0) {
            throw new Error(`Invalid icon map TOML at line ${lineNumber}: ${rawLine}`);
        }
        const key = unquoteKey(line.slice(0, equals).trim());
        const rawValue = line.slice(equals + 1).trimStart();
        const source = sources[currentSource] ??= {};
        source[key] = parseStringValue(rawValue, lineNumber, rawLine);
    }
    return sources;
}

export function mergeIconMaps(...maps: Array<IconMap | undefined>): IconMap {
    const merged: IconMap = {};
    for (const map of maps) {
        for (const [source, values] of Object.entries(map ?? {})) {
            merged[source] = { ...(merged[source] ?? {}), ...values };
        }
    }
    return merged;
}

export function resolveIcon(iconMap: IconMap | undefined, source: string, key: string | undefined | null): string | undefined {
    const trimmed = key?.trim();
    return trimmed ? iconMap?.[source]?.[trimmed] : undefined;
}

function findUnescapedEquals(line: string): number {
    let escaped = false;
    let inString = false;
    for (let index = 0; index < line.length; index += 1) {
        const character = line[index];
        if (escaped) {
            escaped = false;
            continue;
        }
        if (character === "\\") {
            escaped = true;
            continue;
        }
        if (character === '"') {
            inString = !inString;
            continue;
        }
        if (!inString && character === "=") {
            return index;
        }
    }
    return -1;
}

function unquoteKey(key: string): string {
    return key.startsWith('"') && key.endsWith('"') ? key.slice(1, -1) : key;
}

function parseStringValue(rawValue: string, lineNumber: number, rawLine: string): string {
    if (!rawValue.startsWith('"')) {
        throw new Error(`Invalid icon map TOML at line ${lineNumber}: ${rawLine}`);
    }
    let escaped = false;
    for (let index = 1; index < rawValue.length; index += 1) {
        const character = rawValue[index];
        if (escaped) {
            escaped = false;
            continue;
        }
        if (character === "\\") {
            escaped = true;
            continue;
        }
        if (character === '"') {
            const trailing = rawValue.slice(index + 1).trim();
            if (trailing && !trailing.startsWith("#")) {
                throw new Error(`Invalid icon map TOML at line ${lineNumber}: ${rawLine}`);
            }
            return unescapeTomlString(rawValue.slice(1, index), lineNumber, rawLine);
        }
    }
    throw new Error(`Invalid icon map TOML at line ${lineNumber}: ${rawLine}`);
}

function unescapeTomlString(value: string, lineNumber: number, rawLine: string): string {
    let result = "";
    for (let index = 0; index < value.length; index += 1) {
        const character = value[index];
        if (character !== "\\") {
            result += character;
            continue;
        }
        index += 1;
        if (index >= value.length) {
            throw new Error(`Invalid icon map TOML at line ${lineNumber}: ${rawLine}`);
        }
        const escaped = value[index];
        switch (escaped) {
            case "n":
                result += "\n";
                break;
            case "r":
                result += "\r";
                break;
            case "t":
                result += "\t";
                break;
            case '"':
                result += '"';
                break;
            case "\\":
                result += "\\";
                break;
            case "u":
            case "U": {
                const length = escaped === "u" ? 4 : 8;
                const hex = value.slice(index + 1, index + 1 + length);
                if (hex.length !== length || !/^[0-9A-Fa-f]+$/.test(hex)) {
                    throw new Error(`Invalid icon map TOML at line ${lineNumber}: ${rawLine}`);
                }
                const codePoint = Number.parseInt(hex, 16);
                result += String.fromCodePoint(codePoint);
                index += length;
                break;
            }
            default:
                throw new Error(`Invalid icon map TOML at line ${lineNumber}: ${rawLine}`);
        }
    }
    return result;
}
