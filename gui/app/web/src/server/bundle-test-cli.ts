#!/usr/bin/env node
import { createWriteStream } from "node:fs";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import type { ValueMap } from "../../../shared/types.js";
import { errorMessage } from "./errors.js";
import { resolveBundleSourceRoot } from "./bundle-loader.js";
import { loadBundleTestPlan, runBundleTest, writeBundleTestReport } from "./bundle-test-runner.js";
import type { BundleTestInputs, BundleTestStep } from "./bundle-test-types.js";

type CLIInputs = {
    fieldValues: ValueMap;
    configValues: ValueMap;
    checkedOptions: Record<string, string[]>;
};

type BundleTestArgs = {
    action: string[];
    bundle?: string;
    dryRun: boolean;
    help: boolean;
    inputs: CLIInputs;
    log?: string;
    plan?: string;
    quiet: boolean;
    report?: string;
    runSetup: boolean;
    workspace?: string;
};

async function main() {
    const args = parseBundleTestArgs(process.argv.slice(2));
    if (args.help) {
        printHelp();
        return;
    }
    if (!args.bundle) {
        throw new Error("Provide a bundle path.");
    }
    const plan = await loadBundleTestPlan(args.plan);
    plan.inputs = mergeInputs(plan.inputs ?? {}, args.inputs);
    plan.steps = [...(plan.steps ?? []), ...cliSteps(args)];
    if (plan.steps.length === 0) {
        throw new Error("Provide --plan, --run-setup, or at least one --action.");
    }

    const stamp = runStamp();
    const reportPath = path.resolve(args.report ?? defaultReportPath(stamp));
    const logPath = path.resolve(args.log ?? defaultLogPath(stamp));
    const logWriter = await createLogWriter(logPath);
    let report: Awaited<ReturnType<typeof runBundleTest>>;
    try {
        report = await runBundleTest(await resolveBundleSourceRoot(args.bundle), plan, {
            dryRun: args.dryRun,
            ...(args.workspace ? { workspaceURL: path.resolve(args.workspace) } : {}),
            progressHandler: (event) => {
                if (event.type === "message") {
                    logWriter.line(event.text);
                    if (!args.quiet) {
                        console.log(event.text);
                    }
                }
                else if (event.type === "command-output") {
                    logWriter.write(event.text);
                    if (!args.quiet) {
                        process.stdout.write(event.text);
                    }
                }
            },
        });
        await writeBundleTestReport(report, reportPath);
        const summary =
            `Bundle test ${report.status}: ${report.summary.passed} passed, ${report.summary.failed} failed, ${report.summary.skipped} skipped.`;
        logWriter.line(summary);
        logWriter.line(`Report: ${reportPath}`);
        logWriter.line(`Log: ${logPath}`);
        if (!args.quiet) {
            console.log(summary);
            console.log(`Report: ${reportPath}`);
            console.log(`Log: ${logPath}`);
        }
    }
    catch (error) {
        const message = `Bundle test failed: ${errorMessage(error)}`;
        logWriter.line(message);
        if (!args.quiet) {
            console.error(message);
        }
        process.exitCode = 1;
        return;
    }
    finally {
        await logWriter.close();
    }
    if (report.status === "failed") {
        process.exitCode = 1;
    }
}

function parseBundleTestArgs(argv: string[]): BundleTestArgs {
    const parsed: BundleTestArgs = {
        action: [],
        dryRun: false,
        help: false,
        inputs: { fieldValues: {}, configValues: {}, checkedOptions: {} },
        quiet: false,
        runSetup: false,
    };
    const readValue = (flag: string, index: number) => {
        const next = argv[index];
        if (!next || next.startsWith("--")) {
            throw new Error(`Missing value for ${flag}`);
        }
        return next;
    };
    for (let index = 0; index < argv.length; index += 1) {
        const arg = argv[index];
        if (arg == null) {
            break;
        }
        if (arg === "--help" || arg === "-h") {
            parsed.help = true;
        }
        else if (arg === "--plan") {
            parsed.plan = path.resolve(readValue(arg, ++index));
        }
        else if (arg === "--report") {
            parsed.report = path.resolve(readValue(arg, ++index));
        }
        else if (arg === "--log") {
            parsed.log = path.resolve(readValue(arg, ++index));
        }
        else if (arg === "--workspace") {
            parsed.workspace = readValue(arg, ++index);
        }
        else if (arg === "--dry-run") {
            parsed.dryRun = true;
        }
        else if (arg === "--quiet") {
            parsed.quiet = true;
        }
        else if (arg === "--run-setup") {
            parsed.runSetup = true;
        }
        else if (arg === "--action") {
            parsed.action.push(readValue(arg, ++index));
        }
        else if (arg === "--input") {
            const [key, value] = parseKeyValue(readValue(arg, ++index), arg);
            parsed.inputs.fieldValues[key] = value;
        }
        else if (arg === "--config") {
            const [key, value] = parseKeyValue(readValue(arg, ++index), arg);
            parsed.inputs.configValues[key] = value;
        }
        else if (arg === "--checked") {
            const [key, value] = parseKeyValue(readValue(arg, ++index), arg);
            parsed.inputs.checkedOptions[key] = value.split(",").map((item) => item.trim()).filter(Boolean);
        }
        else if (!arg.startsWith("--") && !parsed.bundle) {
            parsed.bundle = arg;
        }
        else {
            throw new Error(`Unknown argument: ${arg}`);
        }
    }
    return parsed;
}

function parseKeyValue(raw: string, optionName: string): [string, string] {
    const separator = raw.indexOf("=");
    if (separator < 0) {
        throw new Error(`${optionName} values must use key=value.`);
    }
    const key = raw.slice(0, separator).trim();
    if (!key) {
        throw new Error(`${optionName} values must include a non-empty key.`);
    }
    return [key, raw.slice(separator + 1)];
}

function cliSteps(args: BundleTestArgs) {
    const steps: BundleTestStep[] = [];
    if (args.runSetup) {
        steps.push({ kind: "setup" });
    }
    for (const actionID of args.action) {
        steps.push({ kind: "action", actionID });
    }
    return steps;
}

function mergeInputs(base: BundleTestInputs, overrides: CLIInputs): BundleTestInputs {
    return {
        fieldValues: { ...(base.fieldValues ?? {}), ...(overrides.fieldValues ?? {}) },
        configValues: { ...(base.configValues ?? {}), ...(overrides.configValues ?? {}) },
        checkedOptions: { ...(base.checkedOptions ?? {}), ...(overrides.checkedOptions ?? {}) },
    };
}

function printHelp() {
    console.log(`Run a WebUI bundle action test plan and write a JSON report.

Usage:
  node dist/web/src/server/bundle-test-cli.js [options] <bundle>

Options:
  --plan <path>            Path to a JSON bundle test plan.
  --report <path>          Write the JSON report to this path.
  --log <path>             Write the live bundle test console log to this path.
  --workspace <path>       Use this bundle workspace directory for the test run.
  --dry-run                Render setup and action commands without executing them.
  --run-setup              Run bundle setup before any --action steps.
  --action <id>            Action id to run. Repeat for multiple actions.
  --input <key=value>      Set an input field. Repeat for multiple inputs.
  --config <key=value>     Set a config value. Repeat for multiple config values.
  --checked <key=a,b>      Set checkbox selections. Repeat for multiple controls.
  --quiet                  Suppress live progress output.
  -h, --help               Show help information.`);
}

async function createLogWriter(logPath: string) {
    await mkdir(path.dirname(logPath), { recursive: true });
    const stream = createWriteStream(logPath, { encoding: "utf8", flags: "w" });
    return {
        line(message: string) {
            stream.write(`${message}\n`);
        },
        write(message: string) {
            if (message) {
                stream.write(message);
            }
        },
        close() {
            return new Promise<void>((resolve, reject) => {
                const onError = (error: Error) => {
                    stream.off("error", onError);
                    reject(error);
                };
                stream.once("error", onError);
                stream.end(() => {
                    stream.off("error", onError);
                    resolve();
                });
            });
        },
    };
}

function runStamp() {
    return new Date().toISOString().replaceAll(":", "-");
}

function defaultReportPath(stamp: string) {
    return path.join(process.cwd(), `bundle-test-report-${stamp}.json`);
}

function defaultLogPath(stamp: string) {
    return path.join(process.cwd(), `bundle-test-log-${stamp}.log`);
}

main().catch((error) => {
    console.error(`Bundle test failed: ${errorMessage(error)}`);
    process.exit(1);
});
