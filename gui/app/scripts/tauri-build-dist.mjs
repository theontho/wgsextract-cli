#!/usr/bin/env node
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const runnerPath = path.join(scriptDir, "run-tauri.mjs");

if (isMainModule()) {
  await main();
}

export async function main(argv = process.argv.slice(2), env = process.env, platform = os.platform()) {
  const defaultBundles = platformBundles(platform);
  const configuredBundles = parseBundleList(env.TAURI_DIST_BUNDLES || env.TAURI_BUNDLES || "");
  const bundles = configuredBundles.length > 0 ? configuredBundles : defaultBundles;

  if (bundles.length === 0) {
    throw new Error(`No Tauri distribution bundle targets configured for ${platform}`);
  }

  console.log(`Building Tauri distribution bundles: ${bundles.join(", ")}`);
  const plans = distributionBuildPlans(bundles, platform);
  for (const [index, plan] of plans.entries()) {
    const planEnv = distributionPlanEnv(env, plan, index);
    await run(
      process.execPath,
      [runnerPath, "build", "--bundles", plan.bundles.join(","), ...argv],
      planEnv,
    );
  }
}

export function platformBundles(platform) {
  switch (platform) {
    case "darwin":
      return ["app", "dmg"];
    case "linux":
      return ["deb", "rpm", "appimage"];
    case "win32":
      return ["nsis"];
    default:
      return [];
  }
}

export function parseBundleList(value) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function distributionBuildPlans(bundles, platform) {
  if (platform !== "linux") {
    return [{ bundles, env: {} }];
  }
  return bundles.map((bundle) => ({
    bundles: [bundle],
    env: {},
  }));
}

export function distributionPlanEnv(env, plan, index) {
  const planEnv = { ...plan.env, ...env };
  if (plan.env.TAURI_PRODUCT_SUFFIX) {
    planEnv.TAURI_PRODUCT_SUFFIX = plan.env.TAURI_PRODUCT_SUFFIX;
  }
  planEnv.TAURI_CLEAN_RELEASE_BUNDLE = index === 0 ? "1" : "0";
  return planEnv;
}

function isMainModule() {
  return Boolean(process.argv[1]) && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
}

function run(command, args, env = process.env) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { env, stdio: "inherit" });
    child.on("error", reject);
    child.on("exit", (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`${command} exited with ${code}`));
      }
    });
  });
}
