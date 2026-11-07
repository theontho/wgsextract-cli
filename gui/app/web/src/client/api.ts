type ApiOptions = {
    method?: string;
    body?: unknown;
    signal?: AbortSignal;
};

type TokenGlobal = typeof globalThis & { __GFC_API_TOKEN__?: unknown };

let cachedToken: string | undefined;

/** The per-launch API token: injected by the desktop shell, or embedded in index.html for the plain web server. */
function apiToken(): string {
    if (cachedToken === undefined) {
        const injected = (globalThis as TokenGlobal).__GFC_API_TOKEN__;
        cachedToken = typeof injected === "string"
            ? injected
            : globalThis.document?.querySelector<HTMLMetaElement>('meta[name="gfc-api-token"]')?.content ?? "";
    }
    return cachedToken;
}

function requestInit(options: ApiOptions): RequestInit {
    const headers: Record<string, string> = { "x-gfc-token": apiToken() };
    const method = options.method ?? (options.body === undefined ? "GET" : "POST");
    if (method !== "GET") {
        headers["content-type"] = "application/json";
    }
    return {
        method,
        cache: "no-store",
        headers,
        ...(method !== "GET" ? { body: JSON.stringify(options.body ?? {}) } : {}),
        ...(options.signal ? { signal: options.signal } : {}),
    };
}

export async function api<T = unknown>(path: string, options: ApiOptions = {}): Promise<T> {
    const response = await fetch(path, requestInit(options));
    const text = await response.text();
    let body: unknown = null;
    if (text.trim()) {
        try {
            body = JSON.parse(text);
        }
        catch (_error) {
            if (!response.ok) {
                throw new Error(response.statusText || `HTTP ${response.status}`);
            }
            throw new Error(`Expected JSON response from ${path}.`);
        }
    }
    if (!response.ok) {
        throw new Error(responseError(body, response));
    }
    return (body ?? {}) as T;
}

/**
 * POSTs to an NDJSON streaming endpoint and calls `onEvent` for each event. Throws if the stream ends
 * without a `complete` event; aborting `signal` cancels the server-side run.
 */
export async function apiStream<Event extends { type?: unknown }>(
    path: string,
    body: unknown,
    onEvent: (event: Event) => void,
    { signal, label }: { signal?: AbortSignal; label: string },
): Promise<void> {
    const response = await fetch(path, requestInit({ method: "POST", body, ...(signal ? { signal } : {}) }));
    if (!response.ok) {
        const text = await response.text();
        let parsed: unknown = null;
        try {
            parsed = text.trim() ? JSON.parse(text) : null;
        }
        catch {
            parsed = text;
        }
        throw new Error(responseError(parsed, response));
    }
    if (!response.body) {
        throw new Error(`${label} did not include a response body.`);
    }
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    let sawComplete = false;
    const handleLine = (line: string) => {
        if (!line.trim()) {
            return;
        }
        let event: Event;
        try {
            event = JSON.parse(line);
        }
        catch {
            throw new Error(`${label} returned invalid JSON event: ${line.trim().slice(0, 160)}`);
        }
        sawComplete ||= event?.type === "complete";
        onEvent(event);
    };
    while (true) {
        const { value, done } = await reader.read();
        if (done) {
            break;
        }
        buffer += value;
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() ?? "";
        lines.forEach(handleLine);
    }
    handleLine(buffer);
    if (!sawComplete) {
        throw new Error(`${label} ended before completion.`);
    }
}

function responseError(body: unknown, response: Response): string {
    if (body && typeof body === "object" && "error" in body) {
        return String(body.error);
    }
    if (typeof body === "string" && body.trim()) {
        return body;
    }
    return response.statusText || `HTTP ${response.status}`;
}
