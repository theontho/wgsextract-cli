import { homedir } from "node:os";
import { renderedCommand } from "../../../shared/rendering.js";
import type { JobServerSpec, RunProcess } from "../../../shared/types.js";
import { errorMessage } from "./errors.js";
import type { JobServerAddress } from "./job-server-client.js";
import { isPlatformScriptReference, resolvePlatformScriptPath } from "./platform-scripts.js";

const defaultStartupTimeoutSeconds = 60;

export interface JobServer {
    /** The running server's address, starting it on first use; undefined when it can't be started. */
    address: () => Promise<JobServerAddress | undefined>;
    /** Stops the server; the next `address()` starts a new one. */
    stop: () => void;
}

/**
 * Runs the bundle's job server command through `runProcess`, so shutdown and bundle
 * reloads terminate it with every other process. A server that exits is started again
 * on next use.
 */
export function createJobServer(spec: JobServerSpec, bundleRoot: string, runProcess: RunProcess): JobServer {
    let starting: Promise<JobServerAddress | undefined> | undefined;
    let stopper: AbortController | undefined;

    async function start(): Promise<JobServerAddress | undefined> {
        const controller = new AbortController();
        stopper = controller;
        const rendered = renderedCommand(spec.command, { bundleRootPath: bundleRoot, homePath: homedir() });
        const executable = isPlatformScriptReference(rendered.executable, bundleRoot)
            ? await resolvePlatformScriptPath(rendered.executable, bundleRoot)
            : rendered.executable;
        const timeoutMs = (spec.startupTimeoutSeconds ?? defaultStartupTimeoutSeconds) * 1000;
        return new Promise((resolve) => {
            let pending = "";
            let announced = false;
            const fail = (reason: string) => {
                clearTimeout(timer);
                if (!announced) {
                    announced = true;
                    console.warn(`Job server unavailable: ${reason}`);
                    controller.abort();
                    resolve(undefined);
                }
            };
            const timer = setTimeout(() => fail(`no ready line within ${timeoutMs / 1000} seconds`), timeoutMs);
            const onStdout = (text: string) => {
                if (announced) {
                    return;
                }
                // Wrapper scripts may print other lines first.
                const lines = (pending + text).split("\n");
                pending = lines.pop() ?? "";
                const address = lines.map(readyAddress).find((value) => value != null);
                if (address) {
                    announced = true;
                    clearTimeout(timer);
                    resolve(address);
                }
            };
            runProcess(executable, rendered.arguments ?? [], {
                cwd: bundleRoot,
                env: { ...process.env, GUI_FOR_CLI_BUNDLE_ROOT: bundleRoot, GUI_FOR_CLI_BUNDLE_WORKSPACE: bundleRoot },
                signal: controller.signal,
                maxOutputBytes: 64 * 1024,
                onStdout,
            })
                .then((result) => fail(`exited with code ${result.exitCode ?? result.signal}: ${result.stderr.trim().slice(-500)}`))
                .catch((error: unknown) => fail(errorMessage(error)))
                .finally(() => {
                    if (stopper === controller) {
                        starting = undefined;
                        stopper = undefined;
                    }
                });
        });
    }

    return {
        address: () => {
            starting ??= start();
            return starting;
        },
        stop: () => {
            stopper?.abort();
            starting = undefined;
            stopper = undefined;
        },
    };
}

function readyAddress(line: string): JobServerAddress | undefined {
    try {
        const value: unknown = JSON.parse(line);
        if (value && typeof value === "object" && "url" in value && "token" in value) {
            const { url, token } = value;
            if (typeof url === "string" && /^https?:\/\//.test(url) && typeof token === "string" && token) {
                return { url: url.replace(/\/+$/, ""), token };
            }
        }
    }
    catch {
        // Not JSON; reported by the caller.
    }
    return undefined;
}
