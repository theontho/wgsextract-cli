import type { ActionSpec, BundleManifest, CommandSpec, LooseRecord } from "./types.js";

/** Result document formats the app can read (docs/result-documents.md). */
export const resultDocumentFormats = ["wgsextract.result/1"] as const;

export type ResultDocumentFormat = (typeof resultDocumentFormats)[number];

/** A manifest's `resultDocument`: actions running one of `executables` print a result document on stdout. */
export interface ResultDocumentSpec {
    format: ResultDocumentFormat;
    /** Raw `command.executable` values, as written in the pages. */
    executables: string[];
    /** Appended to the command, after its optional arguments, to ask for the document. */
    arguments?: string[];
}

export type ResultScalar = string | number | boolean | null;
export type ResultScalars = Record<string, ResultScalar>;

export interface ResultFile {
    path: string;
    role: string;
    kind: string;
    exists: boolean;
    sizeBytes: number | null;
    metrics: ResultScalars;
}

export interface ResultWarning {
    code: string;
    message: string;
    details: ResultScalars;
}

export interface ResultError {
    kind: string;
    message: string;
}

/** The parts of a result document the app shows and checks. */
export interface ResultDocument {
    format: ResultDocumentFormat;
    command: string;
    status: string;
    exitCode: number | null;
    error: ResultError | null;
    outputs: ResultFile[];
    metrics: ResultScalars;
    warnings: ResultWarning[];
    /** Where the command saved the same document, if it did. */
    manifestPath: string | null;
    /** The document as the command printed it. */
    raw: LooseRecord;
}

/** What a failed run asks of the user: fix the options (usage) or install something (missing_dependency). */
export interface ResultNotice {
    kind: "usage" | "missing_dependency";
    message: string;
}

// 127 is the shell's "command not found", which a wrapper script also uses when the runtime is missing.
const exitCodeErrorKinds: Record<number, ResultNotice["kind"]> = { 2: "usage", 3: "missing_dependency", 127: "missing_dependency" };

/** The manifest's result document declaration when it covers this action's command. */
export function resultDocumentSpecFor(manifest: Pick<BundleManifest, "resultDocument"> | null | undefined, action: ActionSpec): ResultDocumentSpec | undefined {
    const spec = manifest?.resultDocument;
    return spec?.executables.includes(action.command?.executable) ? spec : undefined;
}

/** The command with the declaration's arguments added at the end. */
export function commandWithResultArguments(command: CommandSpec, spec: ResultDocumentSpec | undefined): CommandSpec {
    return spec?.arguments?.length
        ? { ...command, optionalArguments: [...(command.optionalArguments ?? []), spec.arguments] }
        : command;
}

/** Parses printed stdout; null when it is not one document of the declared format. */
export function parseResultDocumentText(text: string, format: ResultDocumentFormat): ResultDocument | null {
    try {
        return parseResultDocument(JSON.parse(text.trim()), format);
    }
    catch {
        return null;
    }
}

/**
 * Reads a result document. Fields a job server leaves out when empty read as empty;
 * anything that is not a document of `format` reads as null.
 */
export function parseResultDocument(value: unknown, format: ResultDocumentFormat): ResultDocument | null {
    if (!isRecord(value) || value.schema !== format || typeof value.status !== "string") {
        return null;
    }
    const error = isRecord(value.error) ? { kind: text(value.error.kind), message: text(value.error.message) } : null;
    return {
        format,
        command: text(value.command),
        status: value.status,
        exitCode: typeof value.exit_code === "number" ? value.exit_code : null,
        error,
        outputs: records(value.outputs).map(resultFile).filter((file) => file.path),
        metrics: scalars(value.metrics),
        warnings: records(value.warnings).map((warning) => ({
            code: text(warning.code) || "warning",
            message: text(warning.message),
            details: scalars(warning.details),
        })),
        manifestPath: typeof value.manifest === "string" && value.manifest ? value.manifest : null,
        raw: value,
    };
}

/**
 * Maps a failed run to what the user should do. The document's error kind decides;
 * without a document (the CLI rejected its arguments before writing one, or a wrapper
 * script refused to run), exit code 2, 3 or 127 does, with the last line of stderr as the message.
 */
export function resultNotice(document: ResultDocument | null, exitCode: number | null, stderr: string): ResultNotice | undefined {
    const kind = document ? document.error?.kind : exitCodeErrorKinds[Number(exitCode)];
    if (kind !== "usage" && kind !== "missing_dependency") {
        return undefined;
    }
    const message = document?.error?.message || lastLine(stderr);
    return { kind, message };
}

/** Paths a document lets the user open: its outputs and its saved manifest. */
export function resultDocumentPaths(document: ResultDocument): string[] {
    return [...document.outputs.map((output) => output.path), ...(document.manifestPath ? [document.manifestPath] : [])];
}

function resultFile(value: LooseRecord): ResultFile {
    return {
        path: text(value.path),
        role: text(value.role),
        kind: text(value.kind),
        exists: value.exists !== false,
        sizeBytes: typeof value.size_bytes === "number" ? value.size_bytes : null,
        metrics: scalars(value.metrics),
    };
}

function scalars(value: unknown): ResultScalars {
    const result: ResultScalars = {};
    if (isRecord(value)) {
        for (const [key, item] of Object.entries(value)) {
            if (item === null || ["string", "number", "boolean"].includes(typeof item)) {
                result[key] = item as ResultScalar;
            }
        }
    }
    return result;
}

function records(value: unknown): LooseRecord[] {
    return Array.isArray(value) ? value.filter(isRecord) : [];
}

function isRecord(value: unknown): value is LooseRecord {
    return value != null && typeof value === "object" && !Array.isArray(value);
}

function text(value: unknown): string {
    return typeof value === "string" ? value : "";
}

function lastLine(value: string): string {
    return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).at(-1) ?? "";
}
