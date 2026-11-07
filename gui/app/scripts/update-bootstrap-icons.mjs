import { copyFile, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const webuiRoot = path.resolve(scriptDir, "..");
const packageRoot = path.join(webuiRoot, "node_modules", "bootstrap-icons");
const sourceFontRoot = path.join(packageRoot, "font");
const vendorRoot = path.join(webuiRoot, "web", "vendor", "bootstrap-icons");
const vendorFontRoot = path.join(vendorRoot, "fonts");
const fullStylesheet = path.join(sourceFontRoot, "bootstrap-icons.css");
const criticalStylesheet = path.join(vendorRoot, "bootstrap-icons-critical.css");

const packageJSON = JSON.parse(await readFile(path.join(packageRoot, "package.json"), "utf8"));

await rm(vendorRoot, { recursive: true, force: true });
await mkdir(vendorFontRoot, { recursive: true });

await copyFile(fullStylesheet, path.join(vendorRoot, "bootstrap-icons.css"));
await copyFile(path.join(sourceFontRoot, "fonts", "bootstrap-icons.woff"), path.join(vendorFontRoot, "bootstrap-icons.woff"));
await copyFile(path.join(sourceFontRoot, "fonts", "bootstrap-icons.woff2"), path.join(vendorFontRoot, "bootstrap-icons.woff2"));
await copyFile(path.join(packageRoot, "LICENSE"), path.join(vendorRoot, "LICENSE"));
await writeFile(
  path.join(vendorRoot, "VERSION.json"),
  `${JSON.stringify({ package: "bootstrap-icons", version: packageJSON.version }, null, 2)}\n`,
  "utf8",
);

const fullCSS = await readFile(fullStylesheet, "utf8");
const iconsSource = await readFile(path.join(webuiRoot, "web", "src", "client", "icons.ts"), "utf8");
const bootstrapMapMatch = /export const bootstrapIconMap = \{([\s\S]*?)\n\};/.exec(iconsSource);
if (!bootstrapMapMatch) {
  throw new Error("Failed to extract bootstrapIconMap from web/src/client/icons.ts");
}

const iconNames = new Set([...bootstrapMapMatch[1].matchAll(/:\s*"([^"]+)"/g)].map((match) => match[1]));
iconNames.add("copy");

const firstIconRule = /^\.bi-[\w-]+::before \{/m.exec(fullCSS);
if (!firstIconRule?.index) {
  throw new Error("Failed to locate Bootstrap Icons rule prelude");
}

const prelude = fullCSS
  .slice(0, firstIconRule.index)
  .replace(
    /\/\*![\s\S]*?\*\//,
    `/*!
 * Bootstrap Icons v${packageJSON.version} critical subset for GUI for CLI.
 * Copyright 2019-2024 The Bootstrap Authors
 * Licensed under MIT (https://github.com/twbs/icons/blob/main/LICENSE)
 */`,
  );

const fullLines = fullCSS.split("\n");
const criticalRules = [];
for (const iconName of [...iconNames].sort()) {
  const rule = fullLines.find((line) => line.startsWith(`.bi-${iconName}::before `));
  if (!rule) {
    throw new Error(`Failed to locate Bootstrap Icon rule for ${iconName}`);
  }
  criticalRules.push(rule);
}

await writeFile(criticalStylesheet, `${prelude}${criticalRules.join("\n")}\n`, "utf8");

console.log(`Vendored Bootstrap Icons ${packageJSON.version} into ${path.relative(webuiRoot, vendorRoot)}`);
