import { readFile } from "node:fs/promises";
import path from "node:path";
import { parseJsonWithComments } from "../dist/shared/json-comments.js";

export async function loadBundleMetadata(bundleRoot) {
  const manifestPath = path.join(bundleRoot, "manifest.json");
  const manifest = parseJsonWithComments(await readFile(manifestPath, "utf8"));
  return {
    id: stringValue(manifest.id),
    displayName: stringValue(manifest.displayName),
    version: stringValue(manifest.version),
  };
}

export function packageFileStem(value) {
  const stem = value
    .trim()
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return stem || "app";
}

function stringValue(value) {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}
