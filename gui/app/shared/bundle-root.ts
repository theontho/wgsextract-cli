import { existsSync } from "node:fs";
import path from "node:path";

export function resolveBundleRoot(value: string | undefined, repoRoot: string): string {
    if (!value) {
        return path.join(repoRoot, "examples", "WGSExtract");
    }
    if (path.isAbsolute(value)) {
        return value;
    }
    const cwdCandidate = path.resolve(value);
    if (existsSync(path.join(cwdCandidate, "manifest.json"))) {
        return cwdCandidate;
    }
    return path.resolve(repoRoot, value);
}
