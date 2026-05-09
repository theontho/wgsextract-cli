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
- Lock files: `uv.lock`, `pixi.lock`
- Throwaway dirs: `out/`, `tmp/`, `scratch/`
