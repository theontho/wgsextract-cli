import type { SetupStep } from "./types.js";

export type SetupPlatform = "macos" | "windows" | "linux" | "posix";

export function currentSetupPlatform(platform = runtimePlatform()): SetupPlatform {
  return setupPlatformAlias(platform) ?? "posix";
}

export function setupStepAppliesToPlatform(
  step: Pick<SetupStep, "platforms">,
  platform: SetupPlatform = currentSetupPlatform()
): boolean {
  if (!Array.isArray(step.platforms) || step.platforms.length === 0) {
    return true;
  }
  const platforms = setupPlatforms(step.platforms);
  return platforms.some((candidate) => setupPlatformMatches(candidate, platform));
}

export function setupStepsForPlatform(
  steps: SetupStep[] = [],
  platform: SetupPlatform = currentSetupPlatform()
): SetupStep[] {
  return steps.filter((step) => setupStepAppliesToPlatform(step, platform));
}

export function setupPlatforms(values: unknown): SetupPlatform[] {
  if (!Array.isArray(values)) {
    return [];
  }
  return values
    .map((value) => setupPlatformAlias(String(value)))
    .filter((value): value is SetupPlatform => value != null);
}

export function setupPlatformAlias(value: string): SetupPlatform | undefined {
  switch (value.trim().toLowerCase()) {
    case "darwin":
    case "mac":
    case "macos":
      return "macos";
    case "win":
    case "win32":
    case "windows":
      return "windows";
    case "linux":
      return "linux";
    case "posix":
      return "posix";
    default:
      return undefined;
  }
}

function setupPlatformMatches(candidate: SetupPlatform, platform: SetupPlatform): boolean {
  if (candidate === "posix") {
    return platform !== "windows";
  }
  return candidate === platform;
}

function runtimePlatform(): string {
  const processLike = (globalThis as { process?: { platform?: unknown } }).process;
  if (typeof processLike?.platform === "string") {
    return processLike.platform;
  }
  const navigatorLike = (globalThis as { navigator?: { platform?: unknown; userAgent?: unknown } }).navigator;
  const browserPlatform = `${String(navigatorLike?.platform ?? "")} ${String(navigatorLike?.userAgent ?? "")}`.toLowerCase();
  if (browserPlatform.includes("mac")) {
    return "darwin";
  }
  if (browserPlatform.includes("win")) {
    return "win32";
  }
  if (browserPlatform.includes("linux")) {
    return "linux";
  }
  return "posix";
}
