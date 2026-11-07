# Developer setup

The app lives in the `gui/` folder of the wgsextract-cli repository. Clone it,
then run one command from `gui/` (or `just gui-setup` from the root):

```sh
git clone https://github.com/WGSExtract/wgsextract-cli.git
cd wgsextract-cli/gui
make setup
```

On Linux/macOS without `make`, use `sh scripts/dev_setup.sh` instead.
On Windows, use **Windows PowerShell 5.1**, not WSL:

```powershell
git clone https://github.com/WGSExtract/wgsextract-cli.git
cd wgsextract-cli\gui
powershell -ExecutionPolicy Bypass -File .\make.ps1 setup
```

## What setup does

- Installs missing **user-level** uv, Node.js (22+; bootstraps 24.15.0), and
  Rust/Cargo (minimal stable toolchain). uv supplies Python 3.11+ when needed.
  Node archives are verified against the publisher's SHA-256 checksums.
- Runs `uv sync --locked` and `npm --prefix app ci`; does not rewrite lockfiles.
- Checks tool versions and locked Cargo metadata, compiles TypeScript, starts the
  real GitHelper server on an ephemeral loopback port, verifies HTTP 200, and
  stops it. Smoke children use a private app-support container under
  `tmp/dev-setup/`, removed afterward; existing GUI sessions are untouched.
  The smoke log is in `tmp/dev-setup/smoke.log`.
- Advises about GitHub CLI authentication and an optional AI-agent CLI. It does
  **not** install or authenticate these, or change your Git identity.

Safe to rerun. Open a **new terminal** if prompted: setup persists user tool
directories for future terminals, but cannot change its parent's PATH.
The notice appears when tools or persisted shell/PATH settings changed, or the
invoking terminal cannot find uv, Node, or Cargo. This includes an unchanged rerun
in the same terminal after installing a missing system prerequisite.
On Windows, a pre-existing machine-wide Node can shadow the user-installed Node
in a new terminal. Setup warns if that Node is older: remove or upgrade it, or
reorder PATH so the printed setup directory comes first. The warning includes a
session-only PATH command; setup never changes the machine PATH or execution policy.
Existing Bash login profiles (`.bash_profile` / `.bash_login`) are updated too.
Downloads/installers are from the tools' official HTTPS endpoints; review the
scripts before running if your organization requires approval.

Skip only the quick smoke check with `sh scripts/dev_setup.sh --skip-smoke` or
`powershell -ExecutionPolicy Bypass -File scripts\dev_setup.ps1 -SkipSmoke`.
For browser-only work, add `--web-only` / `-WebOnly` to waive desktop prerequisite
checks (Rust is still installed). Default setup exits nonzero if desktop system
prerequisites are missing, after preparing the Web UI.

## Several worktrees on one machine

Each branch can have its own worktree (`make worktree-setup BRANCH=my-change`), so
work in progress on one branch never spills into another. Setup in a new worktree
reuses the per-user toolchains and does not reinstall them. What each worktree does
repeat:

- `npm ci` into its own `app/node_modules`, from npm's shared download cache.
- The first `make build-tauri` or `make tauri`, which compiles the Rust shell and
  its several hundred crates into that worktree's `target/` directory.

The bundled Node runtime the desktop build ships is downloaded once per user, to
`~/Library/Caches/gui-for-cli-dev` (macOS), `%LOCALAPPDATA%\gui-for-cli-dev\Cache`
(Windows), or `$XDG_CACHE_HOME/gui-for-cli-dev` (Linux, default `~/.cache`).

To avoid recompiling the same crates in every worktree, set up
[sccache](https://github.com/mozilla/sccache) once. It caches compiled crates per
user, so a new worktree's first desktop build reuses them:

```sh
cargo install sccache --locked
```

Then add this to `~/.cargo/config.toml` (`%USERPROFILE%\.cargo\config.toml` on Windows):

```toml
[build]
rustc-wrapper = "sccache"
```

Setup prints an advisory until this is configured. Pointing every worktree at one
shared `target-dir` instead is not recommended: Cargo locks the directory, so
builds in different worktrees wait on each other, and alternating branches with
different dependencies rebuilds them anyway.

Most changes don't need a desktop build at all. Changes to TypeScript, bundles, and
localized strings are checked with `make lint`, `make test`, and `make dev`; build the
desktop app only after changing the Tauri shell or packaging.

## Manual prerequisites

Setup never runs sudo, elevates, or installs system packages.
You need Git and a network connection. Linux/macOS also need curl, tar, and a
SHA-256 tool (`sha256sum` or macOS `shasum`).

- **Debian/Ubuntu:** an administrator runs
  `sudo apt-get update && sudo apt-get install build-essential pkg-config libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev patchelf`.
- **Fedora:** an administrator runs
  `sudo dnf install gcc gcc-c++ make pkgconf-pkg-config webkit2gtk4.1-devel gtk3-devel libappindicator-gtk3-devel librsvg2-devel patchelf`.
- **macOS:** `xcode-select --install`, and accept the Command Line Tools dialog.
  Signing/notarization credentials are unnecessary for ordinary development.
- **Windows:** [Git for Windows](https://git-scm.com/download/win),
  [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
  with **Desktop development with C++** and a Windows 10/11 SDK, and
  [WebView2 Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
  (normally included with Windows 11). These may require an administrator.
  Setup checks SDK UM/UCRT libraries separately from the C++ compiler component.

The script detects missing desktop prerequisites and prints installation
instructions; see also the [Tauri prerequisite guide](https://v2.tauri.app/start/prerequisites/).

There is **no required sibling wgsextract-cli checkout** for UI development.
Start with `examples/GitHelper`, which uses Git already on PATH. The default
WGSExtract bundle's genome actions have separate CLI/runtime/data requirements;
install those through its documented bundle setup when you need that workflow.
Developer setup does not download genomes or run extraction.

## Change → check → PR

```sh
git switch -c my-change
make dev BUNDLE=examples/GitHelper
# Open http://127.0.0.1:8787. Edit a bundle page or a localized string.
# Stop the server with Ctrl-C before running the checks.
make lint
make test
git add <the-files-you-changed>
git commit -m "Describe the change"
git push -u origin my-change
gh pr create --fill
```

Windows: replace each `make <target>` with
`powershell -ExecutionPolicy Bypass -File .\make.ps1 <target>`.
For example:

```powershell
powershell -ExecutionPolicy Bypass -File .\make.ps1 dev BUNDLE=examples/GitHelper
powershell -ExecutionPolicy Bypass -File .\make.ps1 lint
powershell -ExecutionPolicy Bypass -File .\make.ps1 test
```

`dev` runs the TypeScript watcher and reloadable server on all three platforms.
Use Ctrl-C to stop both. The process-scoped execution-policy bypass is required
on fresh Windows clients where the default policy is Restricted.
For a fork, push to your fork and open a PR against `WGSExtract/wgsextract-cli`.
The repository's pre-commit hooks (installed by the root's developer setup,
`docs/developer_setup.md`) scan staged changes for secrets and run `make lint`
when `gui/` changes; CI (`.github/workflows/gui-*.yml`) runs lint and tests on
every platform. Run `make build-tauri` after changing the Tauri shell or
packaging. `make tauri` runs the desktop UI.

## Make an AI-directed change

Install and authenticate your preferred agent CLI using its official instructions.
Run it from this repository so it reads `AGENTS.md` / `CLAUDE.md`.
Pick an issue from the
[good-first-issue list](https://github.com/WGSExtract/wgsextract-cli/labels/good%20first%20issue)
and give it a bounded request, for example:

> Fix #N. Read the issue and repository instructions, confirm the behavior,
> implement the acceptance criteria with a regression test, run `make lint` and
> `make test`, and open a PR. Do not merge. Summarize the checks and any limitations.

Review the diff, try the change yourself, and wait for CI before requesting review.
The Developer setup CI matrix exercises this setup on fresh Ubuntu, macOS, and
Windows runners without tool-setup actions, including reruns, a new-shell PATH
check, and a debug desktop build. Hosted runners supply the OS-level prerequisites;
the script itself never installs those.
