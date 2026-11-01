# Developer shortcuts. Each recipe wraps a Pixi task or command, so `pixi run ...`
# keeps working without just. Run `just` to list recipes.

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

# List recipes
default:
    @just --list --unsorted

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
[script("powershell.exe", "-NoLogo", "-NoProfile", "-File")]
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
[script("powershell.exe", "-NoLogo", "-NoProfile", "-File")]
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
