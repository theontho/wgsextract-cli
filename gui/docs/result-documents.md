# Result documents

A command can describe its run as a JSON result document: the files it wrote, warnings, key metrics, and why it failed. When a bundle says which of its commands print one, the app reads the document instead of the log:

- outputs are listed under the run's output, each with **Open** and **Show in Folder**;
- warnings and metrics are shown in the same summary, not left in the log;
- a refused option is shown next to the action's button, and a missing tool offers **Open Setup**;
- bundle test plans can check the document instead of the printed text.

```jsonc
// manifest.json
"resultDocument": {
  "format": "wgsextract.result/1",
  "executables": ["{{bundleRoot}}/scripts/run-tool.sh"],
  "arguments": ["--json"]
}
```

**`executables`** lists `command.executable` values exactly as the page files write them. Every action, row action and data-source row action whose command runs one of them is covered. Setup steps and data sources are not.

**`arguments`** are added after all of the command's other arguments, to ask for the document.

**`format`** is the only format the app reads today: [`wgsextract.result/1`](../../docs/result-manifest.md), the document every `wgsextract` command prints with `--json`.

## Running a covered action

- Stderr streams to the terminal pane as before. Stdout is captured, and read as the document once the command exits.
- If stdout does not hold a document, it is shown in the terminal pane as ordinary output.
- An action that runs as a job on the bundle's [job server](job-server.md) gets its document from the job's `result`.
- **Open** and **Show in Folder** work only for the files a document listed, and for the document's own saved copy (`manifest`). A relative path is relative to the bundle workspace, where actions run.

## Failures

The document's `error.kind` decides what the app shows next to the action's button until it runs again:

| `error.kind` | Shown |
| --- | --- |
| `usage` | "Check the selected options" with `error.message`. |
| `missing_dependency` | "A required tool is not installed" with `error.message`, and **Open Setup**. |
| `failure`, or none | Nothing extra; the terminal pane shows the failure as for any command. |

Without a document — the command rejected its arguments before writing one, or a wrapper script refused to run — the exit code decides: 2 is a usage error, and 3 or 127 a missing tool. The message is the last line of stderr.

## Test plans

An action step can check the document with `expectedResult`:

```jsonc
{
  "kind": "action",
  "actionID": "bam-to-cram",
  "expectedResult": { "status": "ok", "outputs": ["alignment"] }
}
```

- `status` must equal the document's `status`.
- Each role in `outputs` must be listed, and the file must exist with the size the document recorded.
- `errorKind` must equal `error.kind`. Use it with `expectedExitCodes`, such as `[2]` for a usage error.

The step report keeps the document under `result`. `requiredOutput` and `forbiddenOutput` still match the terminal output, which no longer includes a covered action's stdout.
