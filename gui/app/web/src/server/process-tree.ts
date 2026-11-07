import { spawn } from "node:child_process";
import { platform } from "node:os";
import { setTimeout as delay } from "node:timers/promises";
import { errnoCode, errorMessage } from "./errors.js";

const groupPollIntervalMs = 50;

/**
 * Terminates a spawned process and everything it started.
 *
 * POSIX children are spawned as process-group leaders, so the whole group gets
 * SIGTERM, then SIGKILL once `graceMs` passes without the group exiting.
 * Windows has no process groups; `taskkill /T /F` walks the tree instead.
 */
export async function terminateProcessTree(pid: number, graceMs: number): Promise<void> {
    if (platform() === "win32") {
        await taskkillTree(pid);
        return;
    }
    signalGroup(pid, "SIGTERM");
    const deadline = Date.now() + graceMs;
    while (Date.now() < deadline) {
        if (!groupAlive(pid)) {
            return;
        }
        await delay(groupPollIntervalMs);
    }
    signalGroup(pid, "SIGKILL");
}

function signalGroup(pid: number, signal: NodeJS.Signals) {
    try {
        process.kill(-pid, signal);
    }
    catch (error) {
        if (errnoCode(error) === "ESRCH") {
            return;
        }
        // The group may contain processes we may not signal (for example sudo); still signal our own child.
        try {
            process.kill(pid, signal);
        }
        catch (fallbackError) {
            if (errnoCode(fallbackError) !== "ESRCH") {
                console.warn(`Could not send ${signal} to ${pid}: ${errorMessage(fallbackError)}`);
            }
        }
    }
}

function groupAlive(pid: number): boolean {
    try {
        process.kill(-pid, 0);
        return true;
    }
    catch (error) {
        return errnoCode(error) === "EPERM";
    }
}

function taskkillTree(pid: number): Promise<void> {
    return new Promise((resolve) => {
        const killer = spawn("taskkill.exe", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore", windowsHide: true });
        killer.once("error", (error) => {
            console.warn(`Could not run taskkill for ${pid}: ${errorMessage(error)}`);
            resolve();
        });
        killer.once("exit", () => resolve());
    });
}
