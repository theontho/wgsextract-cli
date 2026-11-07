import type { Labels, SetupStep } from "../../../../shared/types.js";
export function setupToolSummary(step: SetupStep, labels: Labels): string {
  const name = String(step.toolName ?? "").trim();
  const version = String(step.toolVersion ?? "").trim();
  const toolLabel = labels.setupToolLabel ?? "Tool";
  const versionLabel = labels.setupVersionLabel ?? "Version";
  if (name && version) {
    return `${toolLabel}: ${name} ${version}`;
  }
  if (name) {
    return `${toolLabel}: ${name}`;
  }
  if (version) {
    return `${versionLabel}: ${version}`;
  }
  return "";
}
