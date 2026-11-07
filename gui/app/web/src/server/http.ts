import { createReadStream } from "node:fs";
import { readFile, stat } from "node:fs/promises";
import type { Stats } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import path from "node:path";
import { errnoCode, HttpError } from "./errors.js";

const apiTokenMetaPlaceholder = '<meta name="gfc-api-token" content="">';

export async function readJSONBody(request: IncomingMessage, maxBodyBytes: number): Promise<unknown> {
    const chunks: Buffer[] = [];
    let size = 0;
    for await (const chunk of request) {
        const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        size += buffer.length;
        if (size > maxBodyBytes) {
            throw new HttpError(413, "Request body is too large.");
        }
        chunks.push(buffer);
    }
    if (!chunks.length) {
        return {};
    }
    try {
        return JSON.parse(Buffer.concat(chunks).toString("utf8"));
    }
    catch {
        throw new HttpError(400, "Request body is not valid JSON.");
    }
}

/** Serves index.html, embedding the API token only when the page itself has to bootstrap it. */
export async function serveIndex(filePath: string, apiToken: string | undefined, response: ServerResponse, headOnly: boolean) {
    const template = await readFile(filePath, "utf8");
    if (!template.includes(apiTokenMetaPlaceholder)) {
        throw new Error(`${filePath} is missing the API token placeholder.`);
    }
    const html = template.replace(apiTokenMetaPlaceholder, `<meta name="gfc-api-token" content="${apiToken ?? ""}">`);
    response.writeHead(200, {
        "content-type": "text/html; charset=utf-8",
        "content-length": Buffer.byteLength(html),
        "cache-control": "no-store",
    });
    response.end(headOnly ? undefined : html);
}

export async function staticFile(filePath: string, type: string, response: ServerResponse, headOnly = false) {
    const info = await fileInfo(filePath);
    if (!info?.isFile()) {
        notFound(response);
        return;
    }
    response.writeHead(200, {
        "content-type": type,
        "content-length": info.size,
        "cache-control": "no-cache",
    });
    if (headOnly) {
        response.end();
        return;
    }
    streamFile(filePath, response);
}

export async function fileInfo(filePath: string): Promise<Stats | undefined> {
    try {
        return await stat(filePath);
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT" || errnoCode(error) === "ENOTDIR") {
            return undefined;
        }
        throw error;
    }
}

export function streamFile(filePath: string, response: ServerResponse) {
    const stream = createReadStream(filePath);
    stream.on("error", (error) => {
        if (!response.headersSent) {
            response.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
            response.end("Could not read file");
            return;
        }
        response.destroy(error);
    });
    stream.pipe(response);
}

export function json(response: ServerResponse, body: unknown, statusCode = 200) {
    response.writeHead(statusCode, {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store, max-age=0",
    });
    response.end(JSON.stringify(body ?? null));
}

export function notFound(response: ServerResponse) {
    response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    response.end("Not found");
}

export function contentType(filePath: string) {
    const extension = path.extname(filePath).toLowerCase();
    return ({
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".css": "text/css; charset=utf-8",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".json": "application/json; charset=utf-8",
    }[extension] ?? "application/octet-stream");
}
