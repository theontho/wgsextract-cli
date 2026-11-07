import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { chmod, mkdir, readFile, readdir, rename, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { gunzipSync, inflateRawSync } from "node:zlib";

export type BundleArchiveFormat = "zip" | "tar" | "tgz" | "gzipManifest";

interface ArchiveEntry {
    name: string;
    directory: boolean;
    mode: number;
    data: () => Buffer;
}

/** What a bundle picker offers: a bundle's manifest.json or any archive `bundleArchiveFormat` reads. */
export const BUNDLE_FILE_EXTENSIONS = ["json", "zip", "tar", "tgz", "gz", "gzip"] as const;

const COMPLETE_MARKER = ".gui-for-cli-extracted";
/** Folders archivers add beside the bundle that never hold bundle content. */
const IGNORED_ROOT_ENTRIES = new Set([COMPLETE_MARKER, "__MACOSX"]);

export function bundleArchiveFormat(filePath: string): BundleArchiveFormat | null {
    const name = path.basename(filePath).toLowerCase();
    if (name.endsWith(".zip")) return "zip";
    if (name.endsWith(".tar")) return "tar";
    if (name.endsWith(".tar.gz") || name.endsWith(".tgz") || name.endsWith(".tar.gzip")) return "tgz";
    if (name.endsWith(".gz") || name.endsWith(".gzip")) return "gzipManifest";
    return null;
}

/**
 * Extracts a bundle archive into a cache folder keyed by the archive's path, size, and mtime,
 * and returns the folder holding manifest.json (the archive root, or its single top-level folder).
 */
export async function extractBundleArchive(archivePath: string, format: BundleArchiveFormat, cacheRoot = defaultCacheRoot()): Promise<string> {
    const info = await stat(archivePath);
    const key = createHash("sha256").update(`${path.resolve(archivePath)}|${info.size}|${info.mtimeMs}`).digest("hex").slice(0, 24);
    const destination = path.join(cacheRoot, key);
    if (!existsSync(path.join(destination, COMPLETE_MARKER))) {
        const staging = `${destination}.partial-${process.pid}`;
        await rm(staging, { recursive: true, force: true });
        await mkdir(staging, { recursive: true });
        try {
            await writeEntries(staging, archiveEntries(await readFile(archivePath), format));
            await writeFile(path.join(staging, COMPLETE_MARKER), "", "utf8");
            await rm(destination, { recursive: true, force: true });
            await rename(staging, destination);
        }
        catch (error) {
            await rm(staging, { recursive: true, force: true });
            throw error;
        }
    }
    return manifestRoot(destination, archivePath);
}

function defaultCacheRoot(): string {
    return path.join(tmpdir(), "gui-for-cli-bundles");
}

function archiveEntries(buffer: Buffer, format: BundleArchiveFormat): ArchiveEntry[] {
    switch (format) {
        case "zip":
            return zipEntries(buffer);
        case "tar":
            return tarEntries(buffer);
        case "tgz":
            return tarEntries(gunzipSync(buffer));
        case "gzipManifest": {
            const manifest = gunzipSync(buffer);
            return [{ name: "manifest.json", directory: false, mode: 0o644, data: () => manifest }];
        }
    }
}

async function writeEntries(destination: string, entries: ArchiveEntry[]): Promise<void> {
    for (const entry of entries) {
        const target = safeEntryPath(destination, entry.name);
        if (target == null) {
            continue;
        }
        if (entry.directory) {
            await mkdir(target, { recursive: true });
            continue;
        }
        await mkdir(path.dirname(target), { recursive: true });
        await writeFile(target, entry.data());
        if (process.platform !== "win32" && entry.mode & 0o111) {
            await chmod(target, 0o755);
        }
    }
}

/** Resolves an archive entry inside the destination, rejecting absolute paths and `..` escapes. */
export function safeEntryPath(destination: string, name: string): string | null {
    const normalized = name.replaceAll("\\", "/").replace(/^\.\/+/, "");
    if (!normalized || normalized === ".") {
        return null;
    }
    if (normalized.startsWith("/") || /^[A-Za-z]:/.test(normalized) || normalized.split("/").includes("..")) {
        throw new Error(`Bundle archive entry escapes the bundle: ${name}`);
    }
    return path.join(destination, ...normalized.split("/").filter(Boolean));
}

async function manifestRoot(destination: string, archivePath: string): Promise<string> {
    if (existsSync(path.join(destination, "manifest.json"))) {
        return destination;
    }
    const children = (await readdir(destination, { withFileTypes: true })).filter((child) => !IGNORED_ROOT_ENTRIES.has(child.name) && !child.name.startsWith("."));
    const [onlyChild] = children;
    if (children.length === 1 && onlyChild?.isDirectory() && existsSync(path.join(destination, onlyChild.name, "manifest.json"))) {
        return path.join(destination, onlyChild.name);
    }
    throw new Error(`Bundle archive has no manifest.json at its root or in a single top-level folder: ${archivePath}`);
}

function tarEntries(buffer: Buffer): ArchiveEntry[] {
    const entries: ArchiveEntry[] = [];
    let offset = 0;
    let longName: string | null = null;
    let paxPath: string | null = null;
    while (offset + 512 <= buffer.length) {
        const header = buffer.subarray(offset, offset + 512);
        if (header.every((byte) => byte === 0)) {
            break;
        }
        const size = parseOctal(header.subarray(124, 136));
        const type = String.fromCharCode(header[156] ?? 0);
        const dataStart = offset + 512;
        if (dataStart + size > buffer.length) {
            throw new Error("Bundle archive is truncated: a TAR entry is shorter than its header declares.");
        }
        const data = buffer.subarray(dataStart, dataStart + size);
        offset = dataStart + Math.ceil(size / 512) * 512;

        if (type === "L") {
            longName = cString(data);
            continue;
        }
        if (type === "x") {
            paxPath = paxValue(data, "path") ?? paxPath;
            continue;
        }
        if (type === "g") {
            continue;
        }
        const prefix = cString(header.subarray(345, 500));
        const headerName = cString(header.subarray(0, 100));
        const name = longName ?? paxPath ?? (prefix ? `${prefix}/${headerName}` : headerName);
        longName = null;
        paxPath = null;
        if (type === "5") {
            entries.push({ name, directory: true, mode: 0o755, data: () => Buffer.alloc(0) });
        }
        else if (type === "0" || type === "\0" || type === "7") {
            entries.push({ name, directory: false, mode: parseOctal(header.subarray(100, 108)), data: () => data });
        }
        // Links and device entries are skipped: bundles ship plain files.
    }
    return entries;
}

function zipEntries(buffer: Buffer): ArchiveEntry[] {
    const end = findEndOfCentralDirectory(buffer);
    const count = buffer.readUInt16LE(end + 10);
    let offset = buffer.readUInt32LE(end + 16);
    if (offset === 0xffffffff || count === 0xffff) {
        throw new Error("ZIP64 bundle archives are not supported.");
    }
    const entries: ArchiveEntry[] = [];
    for (let index = 0; index < count; index++) {
        if (buffer.readUInt32LE(offset) !== 0x02014b50) {
            throw new Error("Bundle archive has a corrupt ZIP central directory.");
        }
        const method = buffer.readUInt16LE(offset + 10);
        const compressedSize = buffer.readUInt32LE(offset + 20);
        const nameLength = buffer.readUInt16LE(offset + 28);
        const extraLength = buffer.readUInt16LE(offset + 30);
        const commentLength = buffer.readUInt16LE(offset + 32);
        const externalAttributes = buffer.readUInt32LE(offset + 38);
        const localOffset = buffer.readUInt32LE(offset + 42);
        const name = buffer.subarray(offset + 46, offset + 46 + nameLength).toString("utf8");
        offset += 46 + nameLength + extraLength + commentLength;

        const directory = name.endsWith("/");
        entries.push({
            name,
            directory,
            mode: (externalAttributes >>> 16) & 0o777,
            data: () => zipEntryData(buffer, localOffset, compressedSize, method, name),
        });
    }
    return entries;
}

function zipEntryData(buffer: Buffer, localOffset: number, compressedSize: number, method: number, name: string): Buffer {
    if (buffer.readUInt32LE(localOffset) !== 0x04034b50) {
        throw new Error(`Bundle archive has a corrupt ZIP entry: ${name}`);
    }
    const dataStart = localOffset + 30 + buffer.readUInt16LE(localOffset + 26) + buffer.readUInt16LE(localOffset + 28);
    const compressed = buffer.subarray(dataStart, dataStart + compressedSize);
    if (method === 0) return compressed;
    if (method === 8) return inflateRawSync(compressed);
    throw new Error(`Unsupported ZIP compression method ${method} for ${name}.`);
}

function findEndOfCentralDirectory(buffer: Buffer): number {
    if (buffer.length < 22) {
        throw new Error("Bundle archive is not a valid ZIP file.");
    }
    const earliest = Math.max(0, buffer.length - 0xffff - 22);
    for (let offset = buffer.length - 22; offset >= earliest; offset--) {
        if (buffer.readUInt32LE(offset) === 0x06054b50) {
            return offset;
        }
    }
    throw new Error("Bundle archive is not a valid ZIP file.");
}

function parseOctal(field: Buffer): number {
    const text = cString(field).trim();
    return text ? Number.parseInt(text, 8) : 0;
}

function cString(field: Buffer): string {
    const end = field.indexOf(0);
    return field.subarray(0, end === -1 ? field.length : end).toString("utf8");
}

function paxValue(data: Buffer, key: string): string | null {
    for (const record of data.toString("utf8").split("\n")) {
        const match = /^\d+ ([^=]+)=(.*)$/.exec(record);
        if (match?.[1] === key) {
            return match[2] ?? null;
        }
    }
    return null;
}
