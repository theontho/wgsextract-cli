import { api } from "./api.js";
import { errorMessage } from "./model.js";
import { scheduleRender } from "./rerender.js";
import { state } from "./state.js";
import { appendTerminal } from "./terminal.js";
import type { PathPickResponse, WorkspaceLocationInfo } from "../../../shared/types.js";

export function bindWorkspaceLocationEvents(app: HTMLElement) {
    app.querySelector<HTMLInputElement>("[data-workspace-location-input]")?.addEventListener("change", async (event) => {
        await saveWorkspaceLocation((event.currentTarget as HTMLInputElement).value);
    });
    app.querySelector("[data-workspace-location-choose]")?.addEventListener("click", async () => {
        await chooseWorkspaceLocation();
    });
    app.querySelector("[data-workspace-location-reset]")?.addEventListener("click", async () => {
        await saveWorkspaceLocation("");
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
                defaultPath: location?.configuredDirectory ?? location?.defaultDirectory ?? "",
            },
        });
        if (!result.cancelled && result.path) {
            await saveWorkspaceLocation(result.path);
        }
    }
    catch (error) {
        reportFailure(error);
    }
}

async function saveWorkspaceLocation(directory: string) {
    try {
        state.workspaceLocation = await api<WorkspaceLocationInfo>("/api/workspace-location/save", { method: "POST", body: { directory } });
    }
    catch (error) {
        reportFailure(error);
    }
    scheduleRender();
}

function reportFailure(error: unknown) {
    appendTerminal("error", state.labels.workspaceLocationTitle, errorMessage(error));
    scheduleRender();
}
