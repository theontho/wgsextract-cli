# Developer setup

## Set up a machine

macOS or Linux:

```bash
git clone https://github.com/theontho/wgsextract-cli.git
cd wgsextract-cli
./scripts/dev_setup.sh
```

Windows (PowerShell; no WSL or MSYS2 needed):

```powershell
git clone https://github.com/theontho/wgsextract-cli.git
cd wgsextract-cli
powershell -ExecutionPolicy Bypass -File scripts\dev_setup.ps1
```

The script needs only git (and the Xcode Command Line Tools on macOS). It:

1. Installs [Pixi](https://pixi.sh) for your user if it's missing. Pixi provides Python and every bioinformatics tool.
2. Installs the `default` and `dev` Pixi environments exactly as locked in `pixi.lock`. The first run downloads a few GB.
3. Installs [just](https://just.systems) with `pixi global install just` if it's missing.
4. Installs the pre-commit hooks (ruff, shellcheck, whitespace).
5. Runs `wgsextract --version`, `wgsextract deps check` and the quick smoke tests.
6. Lists anything else worth having: a git identity, the GitHub CLI (`gh`), and an AI coding agent.

`deps check` marks tools in optional Pixi environments (GATK, VEP, DeepVariant, ...) as missing. That's expected: install one when you need it with the `pixi install` command `deps check` prints. When the script installs Pixi or `just`, open a new terminal before using them (or add `~/.pixi/bin`, `%USERPROFILE%\.pixi\bin` on Windows, to `PATH`). Rerun the script at any time; `just setup` does the same once `just` is installed. `--skip-tests` (`-SkipTests` on Windows) skips the smoke tests.

## Make a change

```bash
git switch -c my-change
# edit code
just run info --input sample.bam   # try the CLI from this checkout
just check                         # format, lint, typecheck, test
just ci                            # what CI runs; do this before opening a PR
git commit -am "Describe the change"
gh pr create --fill
```

`just` lists every recipe. Each wraps a `pixi run` task, so `pixi run test` and friends work without `just`.

## Make a change with an AI agent

The project is developed with AI coding agents. [AGENTS.md](../AGENTS.md) holds the rules they follow: test and run what they write, run ruff and mypy, keep outputs in `out/` or `tmp/`, keep defaults unchanged.

1. Install an agent, for example GitHub Copilot CLI (`npm install -g @github/copilot`), and start it in the repository: `copilot`.
2. Pick an issue from the [good first issue](https://github.com/theontho/wgsextract-cli/labels/good%20first%20issue) list and tell the agent the outcome, how to check it, and where to stop. For example:

   > Fix issue #N. Add tests that fail without the change, run `just check`, then open a pull request that closes the issue.

3. Review what it did: read the diff (`/diff` in Copilot CLI, or `git diff`), and check that the tests would fail without the change. Run the command yourself on real data when you have it.
4. Have it push and open the pull request, then watch CI (`gh pr checks --watch`). Paste failures or review comments back to the agent until it's green.

Keep each change small and state how you'll verify it. Most problems show up when the result is checked against real data, not in review.

## Release a change

Merged changes ship in the next release; see [releasing.md](releasing.md).
