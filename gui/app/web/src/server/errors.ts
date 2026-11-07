export function errnoCode(error: unknown): string | undefined {
    if (error instanceof Error && "code" in error) {
        const code = (error as NodeJS.ErrnoException).code;
        return typeof code === "string" ? code : undefined;
    }
    return undefined;
}

export function isErrnoCode(error: unknown, ...codes: string[]): boolean {
    const code = errnoCode(error);
    return code !== undefined && codes.includes(code);
}

export function errorMessage(error: unknown): string {
    if (error instanceof Error) return error.message;
    if (typeof error === "string") return error;
    try {
        return JSON.stringify(error) ?? String(error);
    } catch {
        return String(error);
    }
}

export function asError(error: unknown): Error {
    return error instanceof Error ? error : new Error(errorMessage(error));
}

/** An error whose message is safe to return to the client with a specific HTTP status. */
export class HttpError extends Error {
    constructor(readonly status: number, message: string) {
        super(message);
        this.name = "HttpError";
    }
}
