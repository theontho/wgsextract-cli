# Developer shortcuts. Each recipe wraps a Pixi task or command, so `pixi run ...`
# keeps working without just. Run `just` to list recipes.

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

# List recipes
default:
    @just --list --unsorted

# Set up a development machine (Pixi, environments, hooks, checks); see docs/developer_setup.md
[unix]
setup *args:
    ./scripts/dev_setup.sh {{args}}

# Set up a development machine (Pixi, environments, hooks, checks); see docs/developer_setup.md
[windows]
setup *args:
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/dev_setup.ps1 {{args}}

# Install the default and dev Pixi environments
install:
    pixi install
    pixi install -e dev

# Run the CLI, e.g. `just run info --input sample.bam`
[positional-arguments]
[unix]
run *args:
    pixi run wgsextract "$@"

# Run the CLI, e.g. `just run info --input sample.bam`
[extension(".ps1")]
[positional-arguments]
[script("powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File")]
[windows]
run *args:
    pixi run wgsextract @args
    exit $LASTEXITCODE

# Show CLI help
help:
    pixi run wgsextract --help

# Unit and integration tests; extra args go to pytest, e.g. `just test -k microarray`
[positional-arguments]
[unix]
test *args:
    pixi run test "$@"

# Unit and integration tests; extra args go to pytest, e.g. `just test -k microarray`
[extension(".ps1")]
[positional-arguments]
[script("powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File")]
[windows]
test *args:
    pixi run test @args
    exit $LASTEXITCODE

# CLI smoke tests
smoke:
    pixi run smoke-test

# Ruff, site-source and GitHub Actions checks
lint:
    pixi run lint

# Format with ruff
fmt:
    pixi run format

# mypy on src/wgsextract_cli
typecheck:
    pixi run typecheck

# Format, lint, typecheck and test
check: fmt lint typecheck test

# Read-only CI checks plus generated small/full-genome integration tests
ci: lint typecheck
    pixi run test --basetemp=out/ci-pytest-tmp
    pixi run test-e2e-fake --basetemp=out/ci-e2e-pytest-tmp

# Check that external tools are installed
deps:
    pixi run deps-check

# Remove gitignored scratch outputs (out/, tmp/)
clean:
    pixi run clean

# WGS Extract app (gui/); see gui/docs/developer_setup.md

# Set up the app's toolchain (uv, Node, Rust) and locked dependencies
[unix]
[working-directory("gui")]
gui-setup *args:
    sh scripts/dev_setup.sh {{args}}

# Set up the app's toolchain (uv, Node, Rust) and locked dependencies
[windows]
[working-directory("gui")]
gui-setup *args:
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/dev_setup.ps1 {{args}}

# Check the app's development tools
[working-directory("gui")]
gui-precheck:
    uv run --locked python tools/tasks.py precheck

# The app's typecheck, bundle validation, locale lint and duplication check
[working-directory("gui")]
gui-lint:
    uv run --locked python tools/tasks.py lint

# The app's TypeScript and Python tooling tests
[working-directory("gui")]
gui-test:
    uv run --locked python tools/tasks.py test

# The app's lint and tests (mirrors GUI CI)
[working-directory("gui")]
gui-ci:
    uv run --locked python tools/tasks.py ci

# Build and serve the app's Web UI in a browser
[working-directory("gui")]
gui-run:
    uv run --locked python tools/tasks.py run web

# Serve the app's Web UI with TypeScript watch and server reload
[working-directory("gui")]
gui-dev:
    uv run --locked python tools/tasks.py run dev

# Run the Tauri desktop app in dev mode
[working-directory("gui")]
gui-tauri:
    uv run --locked python tools/tasks.py run tauri

# Build the Tauri desktop app for this host
[working-directory("gui")]
gui-build-tauri:
    uv run --locked python tools/tasks.py build tauri

# Package the app's installers for this host into gui/out/release/tauri
[working-directory("gui")]
gui-package:
    uv run --locked python tools/tasks.py package tauri

# Regenerate the app's site into gui/site/ (published at /app/ on Pages)
[working-directory("gui")]
gui-site:
    uv run --locked python scripts/build_site.py
