import { readdirSync, statSync, watch } from "node:fs";
import type { FSWatcher } from "node:fs";
import type { ServerResponse } from "node:http";
import path from "node:path";
import { errorMessage } from "./errors.js";

interface DevReloadOptions {
    enabled: boolean;
    distRoot: string;
    webRoot: string;
}

export function createDevReload({ enabled, distRoot, webRoot }: DevReloadOptions) {
    const clients = new Set<ServerResponse>();
    return {
        addClient(response: ServerResponse) {
            response.writeHead(200, {
                "content-type": "text/event-stream; charset=utf-8",
                "cache-control": "no-cache",
                connection: "keep-alive",
            });
            response.write("event: ready\ndata: ok\n\n");
            clients.add(response);
            response.on("close", () => clients.delete(response));
        },
        installWatcher() {
            if (!enabled) {
                return () => { };
            }
            if (process.platform === "win32") {
                return installPollingWatcher([path.join(distRoot, "web", "src", "client"), path.join(distRoot, "shared")], webRoot, clients);
            }
            const installedWatchers: FSWatcher[] = [];
            for (const directory of [path.join(distRoot, "web", "src", "client"), path.join(distRoot, "shared"), webRoot]) {
                try {
                    const directories = watchedDirectories(directory, webRoot);
                    const watchers = directories.map((watchedDirectory) => watch(watchedDirectory.path, {
                        persistent: false,
                        recursive: watchedDirectory.recursive,
                    }, (_event, fileName) => {
                        const name = String(fileName ?? "");
                        if (directory === webRoot && !["index.html", "styles.css"].includes(name)) {
                            return;
                        }
                        notifyClients(clients);
                    }));
                    installedWatchers.push(...watchers);
                }
                catch (error) {
                    console.warn(`Could not watch ${directory}: ${errorMessage(error)}`);
                }
            }
            const close = () => installedWatchers.splice(0).forEach((watcher) => watcher.close());
            process.once("exit", close);
            return close;
        },
    };
}

function installPollingWatcher(sourceDirectories: string[], webRoot: string, clients: Set<ServerResponse>) {
    let previousSnapshot = pollingSnapshot(sourceDirectories, webRoot);
    const pollIntervalMs = pollingIntervalMs(process.env.WEBUI_DEV_RELOAD_POLL_INTERVAL_MS);
    const interval = setInterval(() => {
        const nextSnapshot = pollingSnapshot(sourceDirectories, webRoot);
        if (nextSnapshot !== previousSnapshot) {
            previousSnapshot = nextSnapshot;
            notifyClients(clients);
        }
    }, pollIntervalMs);
    interval.unref();
    const close = () => clearInterval(interval);
    process.once("exit", close);
    return close;
}

function pollingIntervalMs(value: string | undefined) {
    if (value === undefined) {
        return 500;
    }
    const interval = Number(value);
    return Number.isFinite(interval) && interval >= 100 ? interval : 500;
}

function pollingSnapshot(sourceDirectories: string[], webRoot: string) {
    const entries: string[] = [];
    for (const directory of sourceDirectories) {
        entries.push(...recursiveFileSnapshot(directory));
    }
    for (const fileName of ["index.html", "styles.css"]) {
        entries.push(...fileSnapshot(path.join(webRoot, fileName)));
    }
    return entries.sort().join("\n");
}

function recursiveFileSnapshot(directory: string): string[] {
    const entries: string[] = [];
    try {
        for (const entry of readdirSync(directory, { withFileTypes: true })) {
            const entryPath = path.join(directory, entry.name);
            if (entry.isDirectory()) {
                entries.push(...recursiveFileSnapshot(entryPath));
            }
            else if (entry.isFile()) {
                entries.push(...fileSnapshot(entryPath));
            }
        }
    }
    catch (error) {
        entries.push(`${directory}:missing:${errorMessage(error)}`);
    }
    return entries;
}

function fileSnapshot(filePath: string) {
    try {
        const stat = statSync(filePath);
        return [`${filePath}:${stat.mtimeMs}:${stat.size}`];
    }
    catch (error) {
        return [`${filePath}:missing:${errorMessage(error)}`];
    }
}

function watchedDirectories(directory: string, webRoot: string): Array<{ path: string; recursive: boolean }> {
    if (directory === webRoot) {
        return [{ path: directory, recursive: false }];
    }
    if (process.platform === "win32") {
        return [{ path: directory, recursive: true }];
    }
    return recursiveDirectories(directory).map((path) => ({ path, recursive: false }));
}

function recursiveDirectories(directory: string): string[] {
    const directories = [directory];
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
        if (entry.isDirectory()) {
            directories.push(...recursiveDirectories(path.join(directory, entry.name)));
        }
    }
    return directories;
}

function notifyClients(clients: Set<ServerResponse>) {
    for (const client of [...clients]) {
        client.write("event: reload\ndata: changed\n\n");
    }
}
