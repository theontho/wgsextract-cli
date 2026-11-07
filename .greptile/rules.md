# Project Conventions for Code Review

## Python conventions
- Lint/format handled by **ruff** — skip style nits in review
- Type checking via **mypy** — public APIs should be typed
- Tests via **pytest** — cover both happy and error paths
- Prefer `pathlib.Path` over `os.path` string manipulation
- Avoid bare `except:` and overly broad `except Exception:`
- Use explicit error handling — no silent failures

## Security
- **Always redact** keys, tokens, and credentials when displaying or logging configuration
- Never commit secrets

## Out of scope for review
- Style/formatting (ruff handles it)
- Generated/build artifacts: `.venv/`, `dist/`, `build/`, `__pycache__/`, `*.egg-info/`, `site/`
- Lock files: `pixi.lock`
- Throwaway dirs: `out/`, `tmp/`, `scratch/`

## The desktop app (`gui/`)
One TypeScript app packaged with Tauri for Windows, macOS, and Linux.

- `gui/app/shared/` — must run in both Node and browser (no Node-only APIs)
- `gui/app/web/src/server/` — Node backend; only place Node APIs (`fs`, `path`, `process`, `child_process`) may be used
- `gui/app/web/src/client/` — browser UI; TypeScript compiled by `tsc`, no bundler or framework
- `gui/app/web/packagers/tauri/` — Tauri shell (Rust): window, menus, native pickers, updater only
- Every user-visible string goes through the localization tables in `gui/resources/BuiltinStrings`
- Accessibility: every interactive control needs a label/aria-label; prefer semantic HTML
- Tests run via `node --test`; cover both happy and error paths
- Out of scope: `gui/app/dist/`, `**/target/`, `gui/site/`, `node_modules/`
