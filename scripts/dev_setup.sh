#!/bin/sh
# Bootstrap user-owned tools; project setup is shared with Windows.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
case "${1:-}" in
    --help|-h) echo "Usage: sh scripts/dev_setup.sh [--skip-smoke] [--web-only]"; exit 0 ;;
esac
original_path=$PATH
tools_changed=false
finish() {
    status=$?
    trap - 0
    tools_missing=false
    for tool in uv node cargo; do
        if ! (PATH="$original_path"; command -v "$tool") >/dev/null 2>&1; then
            tools_missing=true
            break
        fi
    done
    if [ "$tools_changed" = true ] || [ "$tools_missing" = true ]; then
        echo "Open a new terminal before running make dev (setup changed tools/profiles, or required tools are missing from the invoking PATH)."
    fi
    exit "$status"
}
trap finish 0
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
mkdir -p tmp/dev-setup "$HOME/.local/bin"
export TMPDIR="$PWD/tmp/dev-setup"
for tool in git curl tar; do
    command -v "$tool" >/dev/null 2>&1 || { echo "Install $tool, then rerun setup." >&2; exit 1; }
done
case "$(uname -s)" in
    Linux) os=linux ;;
    Darwin) os=darwin ;;
    *) echo "Use scripts/dev_setup.ps1 on Windows." >&2; exit 1 ;;
esac
case "$(uname -m)" in
    x86_64) arch=x64 ;;
    arm64|aarch64) arch=arm64 ;;
    *) echo "Unsupported Node architecture: $(uname -m)" >&2; exit 1 ;;
esac
if ! command -v uv >/dev/null 2>&1; then
    curl -fsSL https://astral.sh/uv/install.sh -o tmp/dev-setup/uv-install.sh
    UV_INSTALL_DIR="$HOME/.local/bin" sh tmp/dev-setup/uv-install.sh
    tools_changed=true
fi
if ! command -v node >/dev/null 2>&1 || ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)'; then
    version=24.15.0
    archive="node-v$version-$os-$arch.tar.gz"
    curl -fsSL "https://nodejs.org/dist/v$version/$archive" -o "tmp/dev-setup/$archive"
    curl -fsSL "https://nodejs.org/dist/v$version/SHASUMS256.txt" -o tmp/dev-setup/SHASUMS256.txt
    (cd tmp/dev-setup
        grep "  $archive\$" SHASUMS256.txt > node.sha256
        if command -v sha256sum >/dev/null 2>&1; then sha256sum -c node.sha256
        else shasum -a 256 -c node.sha256; fi
    )
    node_root="$HOME/.local/share/gui-for-cli"
    mkdir -p "$node_root"
    tar -xzf "tmp/dev-setup/$archive" -C "$node_root"
    for tool in node npm npx; do
        ln -sf "$node_root/node-v$version-$os-$arch/bin/$tool" "$HOME/.local/bin/$tool"
    done
    tools_changed=true
fi
if ! command -v cargo >/dev/null 2>&1; then
    curl -fsSL https://sh.rustup.rs -o tmp/dev-setup/rustup-init.sh
    sh tmp/dev-setup/rustup-init.sh -y --profile minimal
    tools_changed=true
fi
# Persist Node's bin directory for new login and interactive shells.
path_line='export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"'
login_profile="$HOME/.profile"
if [ -f "$HOME/.bash_profile" ]; then login_profile="$HOME/.bash_profile"
elif [ -f "$HOME/.bash_login" ]; then login_profile="$HOME/.bash_login"; fi
for profile in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc" "$login_profile"; do
    if ! grep -F "$path_line" "$profile" >/dev/null 2>&1; then
        printf '\n# GUI for CLI developer tools\n%s\n' "$path_line" >> "$profile"
        tools_changed=true
    fi
done
uv sync --locked
uv run --locked python scripts/dev_setup.py "$@"
