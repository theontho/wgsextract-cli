import { access } from "node:fs/promises";
import { platform } from "node:os";
import path from "node:path";
import { errnoCode } from "./errors.js";

export async function platformCommand(executable: string, args: string[] = []): Promise<{ executable: string; args: string[] }> {
    if (platform() !== "win32") {
        return posixScriptCommand(executable, args);
    }
    if (executable === "/usr/bin/env") {
        const [tool, ...rest] = args;
        if (!tool) {
            throw new Error("Missing tool for /usr/bin/env.");
        }
        if (tool === "which") {
            const [candidate] = rest;
            if (candidate && isPathLike(candidate)) {
                return windowsPathExistsCommand(candidate);
            }
            return { executable: "where.exe", args: rest };
        }
        return { executable: tool, args: rest };
    }
    if (executable === "/bin/sh") {
        const [script, ...rest] = args;
        if (!script) {
            throw new Error("Missing script for /bin/sh.");
        }
        return windowsScriptCommand(script, rest);
    }
    return windowsScriptCommand(executable, args);
}

function posixScriptCommand(executable: string, args: string[]) {
    const extension = path.extname(executable).toLowerCase();
    if (extension === ".sh") {
        return { executable: "/bin/sh", args: [executable, ...args] };
    }
    if (extension === ".py") {
        return { executable: "python3", args: [executable, ...args] };
    }
    return { executable, args };
}

async function windowsScriptCommand(executable: string, args: string[]) {
    const extension = path.extname(executable).toLowerCase();
    if (extension === ".sh") {
        const powershellScript = executable.slice(0, -3) + ".ps1";
        if (await exists(powershellScript)) {
            return powershellCommand(powershellScript, args);
        }
    }
    if (extension === ".ps1") {
        return powershellCommand(executable, args);
    }
    if (extension === ".py") {
        return { executable: "python", args: [executable, ...args] };
    }
    return { executable, args };
}

function powershellCommand(script: string, args: string[]) {
    return {
        executable: "powershell.exe",
        args: ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script, ...args],
    };
}

function windowsPathExistsCommand(candidate: string) {
    const script = [
        `$candidate = ${powerShellSingleQuotedString(candidate)}`,
        "$extensions = @('', '.exe', '.cmd', '.ps1')",
        "foreach ($extension in $extensions) {",
        "  $probe = [string]::Concat($candidate, $extension)",
        "  if (Test-Path -LiteralPath $probe -PathType Leaf) { Write-Output $probe; exit 0 }",
        "}",
        "exit 1",
    ].join("; ");
    return {
        executable: "powershell.exe",
        args: ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
    };
}

function isPathLike(value: unknown) {
    return typeof value === "string" && (path.isAbsolute(value) || /[\\/]/.test(value));
}

async function exists(filePath: string) {
    try {
        await access(filePath);
        return true;
    }
    catch (error) {
        if (errnoCode(error) === "ENOENT" || errnoCode(error) === "ENOTDIR") {
            return false;
        }
        throw error;
    }
}

export function powerShellSingleQuotedString(value: string) {
    return `'${String(value).replaceAll("'", "''")}'`;
}
