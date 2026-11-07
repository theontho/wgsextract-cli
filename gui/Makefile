.DEFAULT_GOAL := help

PYTHON ?= uv run python
TASKS := $(PYTHON) tools/tasks.py

export BUNDLE PORT RELEASE_DIR

.PHONY: help setup precheck lint test ci build build-tauri run dev tauri package package-windows-cross clean \
	dup dup-ci cloc site

##@ General

help: ## Show available targets.
	@awk 'BEGIN {FS = ":.*##"} /^##@/ {printf "\n%s\n", substr($$0, 5)} /^[a-zA-Z_-]+:.*##/ {printf "  %-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf '\n%s\n' 'Set BUNDLE=<path> and PORT=<port> for run/dev. Windows: powershell -ExecutionPolicy Bypass -File .\make.ps1 <target>.'

setup: ## Bootstrap user tools, install locked dependencies, verify and smoke test.
	sh scripts/dev_setup.sh

precheck: ## Check the local development environment.
	$(TASKS) precheck

##@ Develop

run: ## Build and serve the Web UI in a browser (BUNDLE, PORT).
	$(TASKS) run web

dev: ## Serve the Web UI with TypeScript watch and server reload (BUNDLE, PORT).
	$(TASKS) run dev

tauri: ## Run the Tauri desktop app in dev mode.
	$(TASKS) run tauri

build: ## Compile the TypeScript Web UI.
	$(TASKS) build web

build-tauri: ## Build the Tauri desktop app for this host.
	$(TASKS) build tauri

##@ Quality

lint: ## Typecheck, validate bundles, lint locales, compile Python tooling, check duplication.
	$(TASKS) lint

test: ## Run TypeScript and Python tooling tests.
	$(TASKS) test

ci: ## Run lint and tests (mirrors CI).
	$(TASKS) ci

dup: ## Duplication report with HTML output in out/jscpd/.
	npx --prefix app jscpd --config .jscpd.json .

dup-ci: ## Duplication check with console-only output.
	npx --prefix app jscpd --config .jscpd.json --reporters console .

cloc: ## Count lines of code, excluding gitignored files.
	cloc --vcs=git .

##@ Release

package: ## Build Tauri distributables for this host into out/release/tauri.
	$(TASKS) package tauri

package-windows-cross: ## Cross-build the Windows NSIS installer from macOS/Linux.
	$(TASKS) package tauri-windows-cross

site: ## Regenerate the static project site into site/.
	$(PYTHON) scripts/build_site.py

clean: ## Remove TypeScript, Tauri, and release build outputs.
	$(TASKS) clean
