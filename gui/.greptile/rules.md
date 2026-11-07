# Project Conventions for Code Review

## Project status
Greenfield, unreleased. **No backward compatibility.** When renaming/restructuring, update every call site and test in the same change. Do not leave "legacy path also works" branches.

## Architecture split
One TypeScript app packaged with Tauri for Windows, macOS, and Linux.

- `app/shared/` — must run in both Node and browser (no Node-only APIs)
- `app/web/src/server/` — Node backend; only place Node APIs (`fs`, `path`, `process`, `child_process`) may be used
- `app/web/src/client/` — browser UI; TypeScript compiled by `tsc`, no bundler or framework
- `app/web/packagers/tauri/` — Tauri shell (Rust): window, menus, native pickers, updater only

## App behavior
- Configuration lives in platform-standard Application Support paths
- **Always redact** keys, tokens, and secrets in displayed configuration
- Behavior must hold on Windows, macOS, and Linux
- Verify both interactive and non-interactive behavior of scripts

## Web UI conventions
- Minimal dependencies
- Accessibility: every interactive control needs a label/aria-label; prefer semantic HTML
- Tests run via `node --test`; cover both happy and error paths

## Out of scope for review
- Style/formatting
- Files under `dist/`, `target/`, `site/`, `out/`, `tmp/`, `node_modules/`
