#!/bin/sh
# One-command developer setup for a source checkout (macOS and Linux):
#
#   ./scripts/dev_setup.sh [--skip-tests]
#
# Installs Pixi and `just` for the current user when they're missing, installs
# the default and dev Pixi environments from the lock file, sets up the
# pre-commit hooks, and checks that the CLI, its tools and a quick test suite
# work. Safe to rerun. Windows: scripts\dev_setup.ps1.
set -eu

ROOT="$(CDPATH='' cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RUN_TESTS=1
for arg in "$@"; do
    case "$arg" in
        --skip-tests) RUN_TESTS=0 ;;
        -h | --help)
            sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n' "$arg" >&2
            exit 2
            ;;
    esac
done

step() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

step "Checking prerequisites"
command -v git >/dev/null 2>&1 || fail "git is required: https://git-scm.com/downloads"
if [ "$(uname -s)" = "Darwin" ] && ! xcode-select -p >/dev/null 2>&1; then
    fail "Install the Xcode Command Line Tools first: xcode-select --install"
fi
note "git: $(git --version)"

step "Pixi"
PATH="$HOME/.pixi/bin:$PATH"
export PATH
if ! command -v pixi >/dev/null 2>&1; then
    command -v curl >/dev/null 2>&1 || fail "curl is required to install Pixi: https://pixi.sh"
    note "Installing Pixi for this user (https://pixi.sh)"
    curl -fsSL https://pixi.sh/install.sh | sh
    command -v pixi >/dev/null 2>&1 || fail "Pixi installed but isn't on PATH; open a new terminal and rerun."
fi
note "$(pixi --version)"

step "Installing the default and dev environments (first run downloads a few GB)"
pixi install --locked
pixi install --locked -e dev

step "just (task runner for the recipes in justfile)"
if ! command -v just >/dev/null 2>&1; then
    note "Installing just with 'pixi global install just'"
    pixi global install just
fi
note "just $(just --version | cut -d' ' -f2)"

if [ -d .git ] || [ -f .git ]; then
    step "Installing pre-commit hooks"
    pixi run pre-commit install
fi

step "Checking the CLI and its tools"
pixi run wgsextract --version
pixi run wgsextract deps check

if [ "$RUN_TESTS" -eq 1 ]; then
    step "Running the quick smoke tests"
    pixi run pytest tests/test_smoke.py -q
fi

step "Optional tools for contributing"
missing=0
if [ -z "$(git config user.email || true)" ]; then
    note "Set your git identity: git config --global user.name 'Your Name' && git config --global user.email you@example.com"
    missing=1
fi
if ! command -v gh >/dev/null 2>&1; then
    note "GitHub CLI (pull requests, CI logs): https://cli.github.com, then 'gh auth login'"
    missing=1
elif ! gh auth status >/dev/null 2>&1; then
    note "Log in to GitHub: gh auth login"
    missing=1
fi
if ! command -v copilot >/dev/null 2>&1 && ! command -v claude >/dev/null 2>&1; then
    note "An AI coding agent, e.g. GitHub Copilot CLI: npm install -g @github/copilot"
    missing=1
fi
[ "$missing" -eq 0 ] && note "All present."

cat <<'EOF'

Setup complete. Common commands (run 'just' for the full list):
  just run --help    run the CLI from this checkout
  just test          unit and integration tests
  just check         format, lint, typecheck and test
  just ci            the checks CI runs; run before opening a pull request
See docs/developer_setup.md for the change-to-pull-request walkthrough.
EOF
