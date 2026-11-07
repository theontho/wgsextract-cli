import type { ServerResponse } from "node:http";
import { contentType, fileInfo, notFound, staticFile } from "./http.js";
import { resolveBundlePath } from "./paths.js";

export async function serveBundleFile(response: ServerResponse, relativePath: string, bundleRoot: string, headOnly = false) {
    let filePath: string;
    try {
        filePath = resolveBundlePath(relativePath, bundleRoot);
    }
    catch {
        notFound(response);
        return;
    }
    await staticFile(filePath, contentType(filePath), response, headOnly);
}

export async function serveBundleFavicon(response: ServerResponse, headOnly: boolean, bundleRoot: string) {
    for (const relativePath of ["Assets/favicon.ico", "favicon.ico", "Assets/icon.png"]) {
        const filePath = resolveBundlePath(relativePath, bundleRoot);
        if ((await fileInfo(filePath))?.isFile()) {
            await staticFile(filePath, contentType(filePath), response, headOnly);
            return;
        }
    }
    notFound(response);
}
