// Playwright-driven UI walkthrough for the Tauri-installed Web UI backend.
//
// Required env:
//   TEST_PORT             Port of the running backend (http://127.0.0.1:PORT)
// Optional env:
//   UI_SETUP_TIMEOUT_SECONDS   How long to wait for setup completion (default 1800)
//   UI_PAGES                   Comma-separated list of page ids to visit
//   UI_HEADLESS                "1" to run headless (no recording)
//   UI_VIEWPORT                "WxH" (default 1600x1000)
//   UI_WINDOW_POSITION         "X,Y" (default 120,40)
//   UI_LOG                     Path to also append progress markers to

import { appendFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { createRequire } from "node:module";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, "..");
const playwrightPackageRoot = path.join(repoRoot, "app");
const { chromium } = createRequire(path.join(playwrightPackageRoot, "package.json"))("playwright");

const port = process.env.TEST_PORT;
const token = process.env.TEST_TOKEN;
if (!port || !token) {
  console.error("ui-driver: TEST_PORT and TEST_TOKEN environment variables are required");
  process.exit(2);
}

const setupTimeoutMs = Number(process.env.UI_SETUP_TIMEOUT_SECONDS ?? "1800") * 1000;
const headless = process.env.UI_HEADLESS === "1";
const [vpW, vpH] = (process.env.UI_VIEWPORT ?? "1600x1000").split("x").map((value) => Number(value));
const [winX, winY] = (process.env.UI_WINDOW_POSITION ?? "120,40").split(",").map((value) => Number(value));
const pagesEnv = process.env.UI_PAGES ?? "settings,fastq,library,extract";
const visitPages = pagesEnv.split(",").map((value) => value.trim()).filter(Boolean);
const logPath = process.env.UI_LOG ?? "";
const startedAt = Date.now();

function log(message) {
  const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1);
  const line = `[ui ${elapsed} s] ${message}`;
  console.log(line);
  if (logPath) {
    appendFileSync(logPath, `${line}\n`, "utf8");
  }
}

function marker(status, name) {
  log(`[${status}] ${name}`);
}

const launchArgs = [
  `--window-size=${vpW},${vpH}`,
  `--window-position=${winX},${winY}`,
];
if (!headless) {
  launchArgs.push("--start-maximized");
}

marker("start", "browser-launch");
const browser = await chromium.launch({ headless, args: launchArgs });
const context = await browser.newContext({ viewport: { width: vpW, height: vpH } });
// The app injects the API token into its own webview; do the same for this browser, for this origin only.
await context.addInitScript(({ origin, apiToken }) => {
  if (location.origin === origin) {
    Object.defineProperty(window, "__GFC_API_TOKEN__", { value: apiToken, writable: false, configurable: false });
  }
}, { origin: `http://127.0.0.1:${port}`, apiToken: token });
const page = await context.newPage();
marker("ok", "browser-launch");

try {
  marker("start", "navigate-home");
  await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});
  marker("ok", "navigate-home");

  marker("start", "await-setup-prompt");
  await page.waitForSelector("[data-setup-prompt-run]", { timeout: 30_000 });
  await page.waitForTimeout(2_000);
  marker("ok", "await-setup-prompt");

  marker("start", "run-setup");
  await page.click("[data-setup-prompt-run]");
  await page.waitForFunction(
    () => !document.querySelector(".setup-global-banner"),
    null,
    { timeout: setupTimeoutMs, polling: 1_000 }
  );
  marker("ok", "run-setup");

  await page.waitForTimeout(1_500);

  for (const pageId of visitPages) {
    const locator = page.locator(`[data-page-id="${pageId}"]`).first();
    if ((await locator.count()) === 0) {
      log(`page ${pageId} not present, skipping`);
      continue;
    }
    marker("start", `navigate-${pageId}`);
    await locator.click({ timeout: 10_000 });
    await page.waitForTimeout(2_500);
    marker("ok", `navigate-${pageId}`);
  }

  marker("start", "browser-close");
  await page.waitForTimeout(1_000);
} finally {
  await browser.close();
  marker("ok", "browser-close");
}
