import type { Server } from "node:http";
import { errnoCode } from "./errors.js";

// Upper bound for shutdown; process-tree termination escalates to SIGKILL well before this.
const shutdownDeadlineMs = 10_000;

interface ShutdownControllerOptions {
    getServer: () => Server | undefined;
    terminateAllProcesses: () => Promise<void>;
}

export function createShutdownController({ getServer, terminateAllProcesses }: ShutdownControllerOptions) {
    let isShuttingDown = false;
    const shutdown = (reason: string) => {
        if (isShuttingDown) {
            return;
        }
        isShuttingDown = true;
        const exitCode = reason === "SIGINT" ? 130 : reason === "uncaughtException" ? 1 : 0;
        const exit = () => process.exit(exitCode);
        getServer()?.close();
        setTimeout(exit, shutdownDeadlineMs).unref();
        void terminateAllProcesses().finally(exit);
    };
    return {
        shutdown,
        installShutdownHandlers() {
            for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"] as const) {
                process.once(signal, () => shutdown(signal));
            }
            process.once("beforeExit", () => void terminateAllProcesses());
            process.once("uncaughtException", (error) => {
                console.error(error);
                shutdown("uncaughtException");
            });
        },
        installParentMonitor() {
            const parentPid = Number(process.env.GFC_PARENT_PID ?? "");
            if (!Number.isInteger(parentPid) || parentPid <= 1) {
                return;
            }
            const timer = setInterval(() => {
                try {
                    process.kill(parentPid, 0);
                }
                catch (error) {
                    if (errnoCode(error) === "ESRCH") {
                        shutdown("parentExit");
                    }
                }
            }, 1000);
            timer.unref();
        },
    };
}
