# Prime Directive: Autonomy & Excellence

**BE AGENTIC AND AUTONOMOUS.** Try obvious next steps yourself before asking for permission.

IF WE TELL YOU TO DO SOMETHING AND YOU DON'T DO IT BECAUSE YOU DON'T WANT TO INSTALL SOMETHING YOU HAVE FAILED, PART OF THE JOB IS BEING **INDEPENDENT** AND INSTALLING DEV TOOLING OR WHATEVER ELSE YOU NEED TO DO YOUR JOB!

- **Quality Over Speed:** Prefer the correct, maintainable implementation over the fastest patch.
- **The Trilemma:** In the choice between Good, Performant, and Cheap, pick Good and Performant.
- **Testing:** You are not done until you have verified the work with realistic commands.
- **Persona:** Act as a thoughtful Senior Software Engineer helping another engineer ship.
- **Conciseness:** Keep changes lean and focused.

## Safety & Reversibility

Do not perform actions that could cause irreversible data loss.

- Committing is reversible and acceptable when explicitly requested.
- Installing software is reversable and uninstallable and thus generally safe.
- Booting simulators and emulators is completely safe, it should never be a blocker if none are running, you can figure that out yourself!
- Temporary files in `tmp/` or `out/` are safe to remove if you created them.
- Do not modify or delete unknown data.
- Do not use destructive Git commands unless explicitly requested.

## Greenfield Stage: No Backward Compatibility

This project is in the greenfield stage and has not been released. Do **not** add backward-compatibility shims, legacy fallbacks, or deprecation paths. When changing a format, schema, file layout, or API, update every call site and test in the same change. If a rename or restructure leaves a "legacy path also works" branch behind, delete it. This rule will be relaxed once the project ships and is removed from this file.

## Project Layout

GUI for CLI is one TypeScript app: a Web UI plus a local Node backend, packaged
with Tauri for Windows, macOS, and Linux. Earlier platforms and experiments
(SwiftUI, the Swift CLI, Electron, Rust/Go/C++/.NET/Python renderers,
benchmarks) live on the `old-experimental` branch; do not reintroduce them here.

- `app/shared` — runtime shared by client and server (rendering, localization, types).
- `app/web/src/server` — Node backend: bundle loading, setup, actions, config.
- `app/web/src/client` — browser UI.
- `app/web/packagers/tauri` — Tauri desktop shell (Rust).
- `examples/` — sample bundles; `examples/WGSExtract` is the shipped app.
- `tools/` — Python repo tooling: `tasks.py` (task runner), packaging, localization lint.

## Test Your Work

- Run `make lint` and `make test` (`powershell -ExecutionPolicy Bypass -File .\make.ps1 lint` / `powershell -ExecutionPolicy Bypass -File .\make.ps1 test` on Windows) after changing TypeScript or tooling.
- Run `make build-tauri` only after changing the Tauri shell (`app/web/packagers/tauri`) or packaging scripts. It compiles the Rust shell, which takes a long time in a fresh worktree; TypeScript, bundle, and localization changes are covered by `make lint`, `make test`, and `make run`.
- Use `make run BUNDLE=<path>` to check UI changes in a browser and `make tauri` to check the desktop app.

## Toolchains and Worktrees

- Node, Rust/Cargo, uv, and the platform build tools (Visual Studio Build Tools on Windows) are installed per user, once per machine, and every checkout and worktree shares them. In a new worktree, run `make precheck` before installing anything; only install what it reports missing.
- Per-worktree state is `app/node_modules` (`make setup`) and the Cargo `target/` directory. The bundled Tauri Node runtime download is cached per user and shared.
- If `make setup` advises a Rust compile cache, tell the user rather than editing their `~/.cargo/config.toml` yourself.
- Verify both interactive and non-interactive behavior for scripts and CLIs.

## TypeScript Standards

- npm (`app/package.json`) is the dependency source of truth.
- Keep reusable logic in `shared/` or the server, not in the Tauri shell; the shell only hosts the window, menus, native pickers, and updates.
- Use Node's built-in test runner (`node:test`) for tests under `app/tests`, and add new test files to `scripts/run-node-tests.mjs`.
- Prefer explicit error handling over non-null assertions and unchecked casts.
- Every user-visible string goes through the localization tables in `resources/BuiltinStrings`.

## App Behavior

- The app's own settings live in platform-standard Application Support paths.
- The user's data does not. Everything that can grow large or that the user manages (settings file, outputs, libraries, temporary files, logs) defaults to a visible, non-dot folder in the user's home, `~/wgs-extract`, known as `{{bundleWorkspace}}`. The app is normally installed for one user on a desktop, who may want to move it all at once to a bigger or faster disk from Settings, or point a single folder elsewhere.
- Paths inside the workspace are shown in the GUI as `{{bundleWorkspace}}/...`, so moving it doesn't appear to change them. Settings files keep real paths, because the CLI reads them too.
- Uninstall never deletes `{{bundleWorkspace}}` without asking, wherever it is. The Reference and Genome Libraries in it may be shared.
- Redact keys, tokens, and secrets in displayed configuration.
- Behavior must work the same on Windows, macOS, and Linux; platform-specific script folders in bundles are the extension point.

## Part of the wgsextract-cli repository

This folder is the WGS Extract desktop app inside the wgsextract-cli
repository; the root `AGENTS.md` applies too (commits, pull requests, issue
queue). Run the commands here from `gui/`, or the root's `just gui-*` recipes
from anywhere. The root's pre-commit hooks lint `gui/` changes with
`tools/tasks.py lint`; the GUI's CI is `.github/workflows/gui-*.yml`. The app
ships in the same `vX.Y.Z` release as the CLI it bundles (see
`docs/releasing.md` at the root).

Run `make setup` to prepare the development tools.

## Build and run

- When you are done making changes, stop any running dev app, rebuild, and run it (`make run` or `make tauri`) to show your work.

## NO MEGAFILES

When making code , don't keep on making one file larger and larger, split up your work.  In large 500+ line files, there are often obvious refactor oppertunties and the files are often multi class.  An easy rule of thumb is one file per class unless the class is under 10 lines, is a dataclass / enum, etc.   Don't let the files get to that megafile state in the first place.

## NO COPY-PASTE

Duplication is checked with [jscpd](https://github.com/kucherenko/jscpd). It runs as part of `make lint` and can be run standalone:

- `make dup` — full report (HTML output in `out/jscpd/`)
- `make dup-ci` — console-only, CI-friendly

Threshold is 0.5% duplication; minLines 30, minTokens 70. Configure in `gui/.jscpd.json`. If you add a legitimate clone that can't be deduped, raise the threshold deliberately and document why.
