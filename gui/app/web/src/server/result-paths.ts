import { stat } from "node:fs/promises";
import path from "node:path";
import { resultDocumentPaths, type ResultDocument } from "../../../shared/result-document.js";
import type { RunProcess } from "../../../shared/types.js";
import { errnoCode, HttpError } from "./errors.js";

const maxRememberedPaths = 1000;

/**
 * Paths listed by result documents this server read. Only these can be opened or revealed
 * through the API, so a page cannot ask the app to open arbitrary files.
 */
export class ResultPaths {
    private readonly paths = new Set<string>();

    remember(document: ResultDocument | null | undefined) {
        for (const filePath of document ? resultDocumentPaths(document) : []) {
            this.paths.delete(filePath);
            this.paths.add(filePath);
        }
        for (const stale of [...this.paths].slice(0, -maxRememberedPaths)) {
            this.paths.delete(stale);
        }
    }

    has(filePath: string) {
        return this.paths.has(filePath);
    }
}

/** Opens a path with the system's default app or, with `reveal`, shows it in the file manager. */
export async function openResultPath(filePath: string, reveal: boolean, paths: ResultPaths, runProcess: RunProcess, cwd: string) {
    if (!paths.has(filePath)) {
        throw new HttpError(403, "Only files listed in an action's result can be opened.");
    }
    // A relative path is relative to the action's working directory.
    const absolutePath = path.resolve(cwd, filePath);
    try {
        await stat(absolutePath);
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT" || errnoCode(error) === "ENOTDIR") {
            throw new HttpError(404, `File not found: ${filePath}`);
        }
        throw error;
    }
    const [opener, args] = reveal ? revealCommand(absolutePath) : [systemOpener(), [absolutePath]];
    await runProcess(opener, args, { cwd, env: process.env });
}

export function systemOpener() {
    return process.platform === "win32" ? "explorer.exe" : process.platform === "darwin" ? "/usr/bin/open" : "xdg-open";
}

function revealCommand(filePath: string): [string, string[]] {
    if (process.platform === "darwin") {
        return ["/usr/bin/open", ["-R", filePath]];
    }
    if (process.platform === "win32") {
        return ["explorer.exe", [`/select,${filePath}`]];
    }
    // Linux file managers have no common "select this file" request; open its folder.
    return ["xdg-open", [path.dirname(filePath)]];
}
