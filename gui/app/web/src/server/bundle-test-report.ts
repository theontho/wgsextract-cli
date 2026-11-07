import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import type { BundleTestEmit, BundleTestStep, BundleTestStepReport, BundleTestStepStatus } from "./bundle-test-types.js";

type ReportValues = {
    actionID?: string | undefined;
    command?: string | undefined;
    exitCode?: number | null;
    timedOut?: boolean;
    output?: string;
    error?: string | null;
};

export async function writeBundleTestReport(report: unknown, reportPath: string | undefined): Promise<string> {
    const outputPath = path.resolve(reportPath ?? defaultReportPath());
    await mkdir(path.dirname(outputPath), { recursive: true });
    await writeFile(outputPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
    return outputPath;
}

export function outputExpectationFailure(output: string, step: BundleTestStep): string | null {
    const failures: string[] = [];
    for (const required of step.requiredOutput ?? []) {
        if (!output.includes(required)) {
            failures.push(`Required output was not found: ${required}`);
        }
    }
    for (const forbidden of step.forbiddenOutput ?? []) {
        if (output.includes(forbidden)) {
            failures.push(`Forbidden output was found: ${forbidden}`);
        }
    }
    return failures.length ? failures.join(" ") : null;
}

export function makeReport(step: BundleTestStep, index: number, started: Date, status: BundleTestStepStatus, values: ReportValues = {}): BundleTestStepReport {
    const finished = new Date();
    return {
        index,
        id: step.id,
        kind: step.kind,
        actionID: values.actionID ?? step.actionID,
        status,
        command: values.command,
        exitCode: values.exitCode,
        timedOut: Boolean(values.timedOut ?? false),
        startedAt: started.toISOString(),
        finishedAt: finished.toISOString(),
        durationSeconds: (finished.getTime() - started.getTime()) / 1000,
        output: String(values.output ?? ""),
        error: values.error == null ? null : String(values.error),
    };
}

export function skippedReport(step: BundleTestStep, index: number, reason: string): BundleTestStepReport {
    const now = new Date();
    return {
        index,
        id: step.id,
        kind: step.kind,
        actionID: step.actionID,
        status: "skipped",
        startedAt: now.toISOString(),
        finishedAt: now.toISOString(),
        durationSeconds: 0,
        output: "",
        error: reason,
    };
}

export function emitStepFinished(report: BundleTestStepReport, totalSteps: number, emit: BundleTestEmit) {
    let message = `Step ${report.index}/${totalSteps} ${report.status} in ${report.durationSeconds.toFixed(1)}s`;
    if (report.exitCode != null) {
        message += ` (exit ${report.exitCode})`;
    }
    if (report.error) {
        message += `: ${report.error}`;
    }
    emit({ type: "message", text: message });
}

export function stepDescription(step: BundleTestStep): string {
    if (step.kind === "setup") {
        return step.id ? `setup ${step.id}` : "setup";
    }
    if (step.kind === "dataSource") {
        return `data source ${step.pageID}/${step.controlID}${step.settingID ? `/${step.settingID}` : ""}`;
    }
    const identifier = step.actionID ?? step.id ?? "action";
    return step.pageID ? `action ${identifier} on ${step.pageID}` : `action ${identifier}`;
}

export function setupCommandSummary(count: number): string {
    return `bundle setup (${count} step${count === 1 ? "" : "s"})`;
}

export function setupResultLine(result: { status?: string; exitCode?: number | null; label?: string | undefined; id: string }): string {
    const status = result.status ?? (result.exitCode === 0 ? "ok" : "failed");
    return `[${status}] ${result.label ?? result.id}`;
}

function defaultReportPath() {
    return path.join(process.cwd(), `bundle-test-report-${new Date().toISOString().replaceAll(":", "-")}.json`);
}

export function timestamp() {
    return new Date().toISOString();
}
