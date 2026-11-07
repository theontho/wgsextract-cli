# Server mode: HTTP/JSON and MCP

`wgsextract serve` offers WGS Extract's operations to other programs on the
same computer: desktop apps, scripts and AI agents. It speaks either a small
HTTP/JSON API or the [Model Context Protocol](https://modelcontextprotocol.io)
(MCP) on stdio.

Both are generated from the [typed Python API](api.md): the same `*Params`
fields, defaults, config.toml settings, result manifests and error kinds as
the CLI. So an operation joins the server as soon as its command moves onto
the API. Today they are `info`, `info calculate-coverage`,
`info coverage-sample`, `info chip`, `vcf snp`, `vcf indel`, the three
`lineage` commands, `microarray`, `report` and `sample show`/`set`/`unset`.

```console
$ wgsextract serve --allow ~/wgs
WGS Extract server on http://127.0.0.1:41873/v1
Token: 3q2Kx...
  (also in ~/.../state/server/server.json, readable by you only)
Allowed folders: /home/me/wgs
Try: curl -H 'Authorization: Bearer 3q2Kx...' http://127.0.0.1:41873/v1/ops
Stop with Ctrl-C.
```

## Security

The server runs analyses with your permissions, so it only answers programs
you start yourself:

- It listens on 127.0.0.1 only. There is no option to listen on the network.
- Every request except `GET /v1/health` needs `Authorization: Bearer <token>`.
  The token is new each time the server starts. It is printed to stderr and
  saved in `<state>/server/server.json` (mode 0600), along with the URL and
  process id, so local clients can find it. The file is removed when the
  server stops.
- Requests whose `Host` header isn't `127.0.0.1:<port>` or `localhost:<port>`,
  and requests whose `Origin` is a different site, are refused. This stops a web page
  in your browser from reaching the server through DNS rebinding or
  cross-site requests.
- **Paths are guarded.** Every path parameter must be absolute. Network and
  device paths (`\\host\share`, `//host/share`, `\\?\…`, `\\.\…`) are
  refused on every system before anything is opened, since Windows sends your
  sign-in credentials to any host whose share it opens. A path must lie
  inside an allowed folder as written (with `..` removed); only then are its
  symlinks resolved, and the result must lie inside one too. The
  allowed folders are those given with `--allow`, the `server_allowed_paths`
  setting, and the `genome_library`, `reference_library` and
  `output_directory` settings. Of the settings, only the folders that exist
  are allowed; a missing `--allow` folder stops the server. Folders a
  command derives from a path are checked too: a `ref` FASTA in a
  `genomes` folder names the library above it, which must also be allowed.
  A leading `~` in a path is expanded before it is checked; a text value
  that may name a file (a region, an output name) must give it as an
  absolute path, without `~`. A symlink inside
  an allowed folder that points outside it is refused. That holds for the
  paths a request names; the files a command finds inside a named folder (a
  reference library's genomes, a model folder's weights, a checkpoint's
  files) aren't checked one by one.
  The guard limits what a client can name. It is not a sandbox against other
  programs running as you, which can change the allowed folders (for example
  retarget a symlink) whatever the server does.
- **Paths recorded in manifests.** `report` reads the outputs that earlier
  runs' manifests list. Run by the server, it reads only those inside the
  allowed folders, so a manifest can't lead it to other files.
- **Regions can't pass for options.** A `region` item that starts with `-`
  is refused (on the CLI and the API too), and commands pass regions to
  samtools and tabix after `--`.
- **Deleting needs an opt-in.** Requests that delete data a command didn't
  make are refused (403 `destructive`) unless the server was started with
  `--allow-destructive`: `bam unindex`, `sample unset` and
  `ref gene-map` with `delete`. Over MCP, `bam_unindex` and `sample_unset`
  are offered only then, and their tools carry `destructiveHint`.
  Regenerating a command's own outputs (`restart`, `force_*`) isn't
  deleting in this sense.
- Parameters that name programs to run, such as `haplogrep_path`, or that
  hide paths inside other text, such as `indel_manifest`, can't be set by a
  request. They come from config.toml as usual.
- `ref download`, which fetches any URL, and `ref bootstrap`, which saves
  config.toml, aren't offered.
- Request bodies are limited to 1 MB and must be `application/json`. Unknown
  parameters are rejected rather than ignored.
- Over MCP, only read-only queries are offered unless you pass
  `--allow-jobs`. Without it, they save nothing: parameters that would save
  files (`outdir`, `table_format`, and `info`'s `detailed`, which saves the
  coverage table) are left out of the tool schemas and refused with
  `read_only`, and `info` runs with `cache: false`, so it reads an existing
  `<input>.wgse_info.json` but doesn't write one.
- Job records and logs (`<state>/server/jobs/`) are readable by you only.

## HTTP API

All responses are compact JSON. Errors are
`{"error": {"code": "...", "message": "..."}}` with an HTTP status code:

| Status | Meaning |
| --- | --- |
| 400 | Bad parameters: `usage`, `bad_json` or `bad_length` |
| 401 | Missing or wrong token |
| 403 | A path outside the allowed folders (`path_not_allowed`), a request that deletes data (`destructive`), or a bad Host or Origin |
| 404 | Unknown operation or job |
| 409 | Cancelling a finished job |

A failed analysis is not an HTTP error. Its job finishes with status
`failed` and the manifest's `error`.

| Request | Does |
| --- | --- |
| `GET /v1/health` | `{"ok": true, "version": ...}`; needs no token |
| `GET /v1` | Index and the allowed folders |
| `GET /v1/openapi.json` | The OpenAPI 3.1 document, also `wgsextract serve --openapi` |
| `GET /v1/ops` | Operations: `op`, `kind` (`query` or `job`), `destructive` when a request may delete data, and a one-line summary |
| `GET /v1/ops/{op}` | One operation and the JSON Schema of its parameters |
| `POST /v1/ops/{op}?wait=S` | Start the operation as a job; the body is its parameters |
| `GET /v1/jobs?status=` | Jobs, newest last |
| `GET /v1/jobs/{id}?wait=S&full=1` | A job; with `wait`, block up to S seconds (≤600) for it to finish |
| `GET /v1/jobs/{id}/events` | Server-sent events: status changes and progress; ends when the job does |
| `GET /v1/jobs/{id}/log?tail=N` | The last N lines of the job's log (default 200; 0 for all) |
| `DELETE /v1/jobs/{id}` | Cancel a queued or running job |
| `POST /v1/jobs/{id}/rerun` | Start a new job with the same parameters |
| `GET /v1/manifest?path=&full=1` | A result manifest (`*.manifest.json`); `provenance` only with `full=1` |
| `GET /v1/variants?path=&gene=&rsid=&region=&clinvar=&min_confidence=&columns=&limit=&cursor=` | Rows of a `vcf table` output; see [Lookups](#lookups) |
| `GET /v1/report?path=&section=` | A sample's personal report; see [Lookups](#lookups) |

Operation names are the CLI command with `/` between words: `info`,
`info/chip`, `vcf/snp`, `lineage/mt-haplogroup`, `sample/show`. Parameters
are the API's Params fields, for example:

```sh
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$URL/ops/vcf/snp" -d '{"input": "/home/me/wgs/HG002.cram", "region": "chrM", "ploidy": "1"}'
# 202 {"id":"20260105-100425-986af7","op":"vcf/snp","kind":"job","status":"running",...}
curl -N -H "Authorization: Bearer $TOKEN" "$URL/jobs/20260105-100425-986af7/events"
curl -H "Authorization: Bearer $TOKEN" "$URL/jobs/20260105-100425-986af7?wait=600"
```

`POST` answers 200 if the job has finished by the end of `wait`, otherwise
202. A job record holds `status` (`queued`, `running`, `succeeded`,
`failed`, `cancelled` or `interrupted`), the latest `progress`, the
`exit_code` and `error`, and the operation's result manifest as `result`.
The manifest's `provenance` (tool versions and the full parameters) appears
only with `full=1`, to keep responses short.

### Lookups

The server answers three read-only lookups itself, without a job, over HTTP
(`GET /v1/manifest`, `/v1/variants`, `/v1/report`) and MCP (tools
`read_manifest`, `query_variants`, `report_section`). Their paths must be in
an allowed folder.

- **Variants.** `path` is a [`vcf table`](variant-table.md) output:
  `*_variants.sqlite`, `.tsv` or `.parquet` (needs pyarrow). The filters
  combine: `gene` (symbol), `rsid` (one rsID), `region` (`chr1`,
  `chr1:1000` or `chr1:1000-2000`, named as in the table), `clinvar` (a
  ClinVar significance term such as `Pathogenic`, case-insensitive) and
  `min_confidence` (`low`, `moderate` or `high`). `columns` picks columns
  (comma-separated over HTTP); empty values are left out. An answer has at
  most `limit` rows (default 50, at most 1000); pass its `next_cursor` as
  `cursor` for the next page. The SQLite database is fastest: it uses its
  gene and position indexes, and starts a page at its cursor. A TSV or
  Parquet table is read from the start for every page, so use SQLite for
  paging through large tables. Boolean query parameters take `true`/`false`,
  `yes`/`no` or `1`/`0`.
- **Report.** `path` is a sample folder, whose newest
  `*_personal_report.json` is read, or the report JSON itself (run the
  `report` operation first). Without `section`, the answer lists each
  section's `status` and `summary`; with `section` (for example
  `pharmacogenomics`), it is that section in full, with its findings.

```sh
curl -H "Authorization: ******" \
  "$URL/variants?path=/home/me/wgs/HG002/HG002_variants.sqlite&clinvar=Pathogenic&min_confidence=high"
# 200 {"path": "...", "rows": [{"chrom": "chr17", "pos": 43094464, "gene": "BRCA1", ...}], "next_cursor": null}
```

### Jobs

- **Queries and jobs.** Quick queries (`info`, `info/chip`, `sample/show`,
  `ref/status`) start at once. Other operations queue: `--max-jobs`
  (setting `server_max_jobs`, default 1) of them run at a time. Jobs that save to
  the same folder run one after another, since commands use fixed file
  names there. `ref/index`, `ref/count-ns` and `ref/verify` save beside
  their FASTA, so they need no `outdir`; given a library folder, they also
  hold its `genomes` folder. The library installers hold their library and
  its `genomes` folder.
- **Isolation.** Each job runs the API function in its own worker process.
  A crash or a cancel affects only that job. Its log, at the CLI's log level
  (`--debug` for more), is `<state>/server/jobs/<id>.log`.
- **Cancelling.** A queued job is cancelled immediately. A running job gets
  the API's `CancelToken`: tools stop, and the manifest is written with
  status `cancelled` (exit code 130). If the job is still running 30 seconds
  later, it is killed. As on the CLI, partial outputs are kept unless the
  `atomic_outputs` setting is on.
- **Persistence.** Job records are kept in `<state>/server/jobs/<id>.json`,
  so a restarted server still lists the earlier jobs and their results. Jobs
  that were queued or running when a server stopped are marked
  `interrupted`. Start them again with `rerun`; commands with step-level
  resume ([step-resume.md](step-resume.md)) continue from their last finished
  step. One HTTP server uses a state folder at a time; a second one refuses
  to start.

## MCP

`wgsextract serve --mcp` speaks MCP over stdin and stdout: one JSON-RPC
message per line, protocol versions 2024-11-05 to 2025-11-25. It needs no
extra packages. Logs go to stderr. Configure your client to start it, for
example:

```json
{
  "mcpServers": {
    "wgsextract": {
      "command": "wgsextract",
      "args": ["serve", "--mcp", "--allow", "/home/me/wgs"]
    }
  }
}
```

Tools are named after the operations, with `_` between words: `info`,
`info_chip`, `sample_show`, and with `--allow-jobs` also `vcf_snp`,
`lineage_mt_haplogroup` and the others. Each tool's input schema is its
Params' JSON Schema. These tools are always offered:

- `read_manifest`, `query_variants` and `report_section`: the
  [lookups](#lookups).
- `list_jobs`: list the jobs.
- `job_status`: a job's status and result. `wait_seconds` waits for the job
  to finish.

With `--allow-jobs`, `cancel_job` is offered too. Each MCP session has its
own jobs, so several agents can run servers at once. When the client
disconnects, its running jobs are cancelled and the session's job records
are removed; outputs and their manifests stay where the jobs saved them.

- **Query tools** wait for the result. They send `notifications/progress`
  when the client gives a `progressToken`, and are cancelled by
  `notifications/cancelled`.
- **Job tools** return the started job at once. Follow it with
  `job_status`.

Results are the job record, as `structuredContent` and as compact JSON
text. A failed analysis or a refused path is a result with `isError: true`
that says why, so the agent can correct itself.

## Settings and options

| Setting | Option | Default |
| --- | --- | --- |
| `server_port` | `--port` | 0: any free port |
| `server_allowed_paths` | `--allow DIR` (adds) | none, plus the library and output folders |
| `server_max_jobs` | `--max-jobs` | 1 |
| | `--allow-destructive` | off: requests that delete data are refused |

Workers use the same config.toml as the server (`--config`). A request
parameter that is left out takes its setting, as on the CLI.
