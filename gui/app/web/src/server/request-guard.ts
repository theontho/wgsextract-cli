import { randomBytes, timingSafeEqual } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";
import { HttpError } from "./errors.js";

export const apiTokenHeader = "x-gfc-token";
const apiTokenEnvironmentKey = "GFC_API_TOKEN";
const minimumTokenLength = 32;
const loopbackHostnames = ["127.0.0.1", "localhost", "[::1]"];

export const contentSecurityPolicy = [
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    // Tauri IPC uses the ipc: scheme (or http://ipc.localhost on Windows).
    "connect-src 'self' ipc: http://ipc.localhost",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
    "object-src 'none'",
].join("; ");

/** Takes the launcher-provided token from the environment (so child commands never inherit it) or makes one. */
export function takeApiToken(environment: NodeJS.ProcessEnv = process.env): string {
    const provided = environment[apiTokenEnvironmentKey]?.trim();
    delete environment[apiTokenEnvironmentKey];
    if (provided == null || provided === "") {
        return randomBytes(32).toString("base64url");
    }
    if (provided.length < minimumTokenLength || !/^[A-Za-z0-9_-]+$/.test(provided)) {
        throw new Error(`${apiTokenEnvironmentKey} must be at least ${minimumTokenLength} URL-safe characters.`);
    }
    return provided;
}

export function applySecurityHeaders(response: ServerResponse) {
    response.setHeader("content-security-policy", contentSecurityPolicy);
    response.setHeader("x-content-type-options", "nosniff");
    response.setHeader("referrer-policy", "no-referrer");
    response.setHeader("cross-origin-resource-policy", "same-origin");
    response.setHeader("cross-origin-opener-policy", "same-origin");
}

export interface RequestGuardOptions {
    apiToken: string;
    /** Extra hostnames the server was explicitly bound to, besides the loopback names. */
    hostnames?: string[];
}

/**
 * Throws an HttpError unless the request comes from our own page (or launcher) on this exact origin.
 * The page reads the API token from the served index.html, which a cross-origin page cannot read, and a
 * DNS-rebinding page cannot load because its Host header does not match.
 */
export function guardRequest(request: IncomingMessage, pathname: string, options: RequestGuardOptions) {
    const host = request.headers.host ?? "";
    if (!allowedHosts(request, options.hostnames ?? []).has(host.toLowerCase())) {
        throw new HttpError(403, "Request host is not allowed.");
    }
    const origin = request.headers.origin;
    if (origin != null && origin.toLowerCase() !== `http://${host.toLowerCase()}`) {
        throw new HttpError(403, "Cross-origin requests are not allowed.");
    }
    if (!pathname.startsWith("/api/")) {
        return;
    }
    if (!tokenMatches(request.headers[apiTokenHeader], options.apiToken)) {
        throw new HttpError(401, "Missing or invalid API token.");
    }
    if (request.method === "POST" && mediaType(request.headers["content-type"]) !== "application/json") {
        throw new HttpError(415, "API requests must use Content-Type: application/json.");
    }
}

function allowedHosts(request: IncomingMessage, hostnames: string[]) {
    const port = request.socket.localPort;
    return new Set([...loopbackHostnames, ...hostnames].map((hostname) => `${hostname.toLowerCase()}:${port}`));
}

function tokenMatches(value: string | string[] | undefined, expected: string) {
    if (typeof value !== "string") {
        return false;
    }
    const actual = Buffer.from(value);
    const wanted = Buffer.from(expected);
    return actual.length === wanted.length && timingSafeEqual(actual, wanted);
}

function mediaType(value: string | undefined) {
    return (value ?? "").split(";", 1)[0]?.trim().toLowerCase();
}
