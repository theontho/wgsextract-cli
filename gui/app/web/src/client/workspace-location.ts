import { apiStream, api } from "./api.js";
import { errorMessage } from "./model.js";
import { scheduleRender } from "./rerender.js";
import { state } from "./state.js";
import { appendTerminal } from "./terminal.js";
import type { PathPickResponse } from "../../../shared/types.js";

type WorkspaceMoveEvent =
    | { type: "progress"; percent: number }
    | { type: "complete" }
    | { type: "error"; error: string };

export function bindWorkspaceLocationEvents(app: HTMLElement) {
    app.querySelector<HTMLInputElement>("[data-workspace-location-input]")?.addEventListener("change", (event) => {
        state.workspaceLocationDraft = (event.currentTarget as HTMLInputElement).value.trim();
        scheduleRender();
    });
    app.querySelector("[data-workspace-location-choose]")?.addEventListener("click", async () => {
        await chooseWorkspaceLocation();
    });
    app.querySelector("[data-workspace-location-move]")?.addEventListener("click", () => {
        askToMove(state.workspaceLocationDraft ?? "");
    });
    app.querySelector("[data-workspace-location-reset]")?.addEventListener("click", () => {
        askToMove(null);
    });
    app.querySelector("[data-workspace-location-cancel]")?.addEventListener("click", () => {
        state.workspaceMove = null;
        scheduleRender();
    });
    app.querySelector("[data-workspace-location-confirm]")?.addEventListener("click", async () => {
        await moveWorkspace();
    });
}

async function chooseWorkspaceLocation() {
    const location = state.workspaceLocation;
    try {
        const result = await api<PathPickResponse>("/api/path/pick", {
            method: "POST",
            body: {
                kind: "directory",
                title: state.labels.workspaceLocationFolderLabel,
                defaultPath: state.workspaceLocationDraft || location?.configuredDirectory || location?.defaultDirectory || "",
            },
        });
        if (!result.cancelled && result.path) {
            state.workspaceLocationDraft = result.path;
            scheduleRender();
        }
    }
    catch (error) {
        reportFailure(errorMessage(error));
    }
}

function askToMove(directory: string | null) {
    if (directory !== "") {
        state.workspaceMove = { directory, running: false, percent: 0 };
        scheduleRender();
    }
}

async function moveWorkspace() {
    const move = state.workspaceMove;
    if (!move) {
        return;
    }
    move.running = true;
    scheduleRender();
    let failure: string | null = null;
    try {
        await apiStream<WorkspaceMoveEvent>("/api/workspace-location/move", { directory: move.directory }, (event) => {
            if (event.type === "progress") {
                move.percent = event.percent;
                scheduleRender();
            }
            else if (event.type === "error") {
                failure = event.error;
            }
        }, { label: "Workspace move" });
        // Everything the page holds (paths, settings, setup status) came from the old location.
        window.location.reload();
    }
    catch (error) {
        state.workspaceMove = null;
        reportFailure(failure ?? errorMessage(error));
    }
}

function reportFailure(message: string) {
    appendTerminal("error", state.labels.workspaceLocationTitle, message);
    scheduleRender();
}
