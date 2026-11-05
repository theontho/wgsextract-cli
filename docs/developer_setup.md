# Developer setup

## Set up a machine

macOS or Linux:

```bash
git clone https://github.com/WGSExtract/wgsextract-cli.git
cd wgsextract-cli
./scripts/dev_setup.sh
```

Windows (PowerShell; no WSL or MSYS2 needed):

```powershell
git clone https://github.com/WGSExtract/wgsextract-cli.git
cd wgsextract-cli
powershell -ExecutionPolicy Bypass -File scripts\dev_setup.ps1
```

The script needs only git (and the Xcode Command Line Tools on macOS). It:

1. Installs [Pixi](https://pixi.sh) for your user if it's missing. Pixi provides Python and every bioinformatics tool.
2. Installs the `default` and `dev` Pixi environments exactly as locked in `pixi.lock`. The first run downloads a few GB.
3. Installs [just](https://just.systems) with `pixi global install just` if it's missing.
4. Installs the pre-commit hooks (ruff, shellcheck, gitleaks, whitespace).
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

When you add or rename a command:

- Give a new top-level command a tier (core, setup or advanced) and a short
  summary in `COMMAND_TIERS` in `src/wgsextract_cli/commands/registry.py`.
- Give every runnable command an example and an outputs line in
  `src/wgsextract_cli/commands/_help_catalog.py`.
- Run `pixi run gen-command-docs` to regenerate `docs/commands.md`.

`tests/test_cli_help.py` fails until all three are done.

## Write tests

A good test fails when the behaviour a user relies on breaks, and survives a refactor that keeps that behaviour.

- **Assert observable behaviour.** That means:
  - output files and their content
  - exit codes 0/1/2/3
  - the result manifest
  - structured warnings
  - documented CLI and API behaviour
- **Use real tools on tiny data.** Prefer the fake genomes and fixtures with real samtools and bcftools. The helpers live in `tests/htslib_helpers.py`, `tests/fake_tools.py` and `tests/smoke_utils.py`. For Pixi environments, the `fake_pixi` fixture runs a stand-in pixi with fake environments in a temporary folder. For disk-space preflights, `tests/disk_volumes.Volumes` puts folders on fake volumes with a set amount of free space and makes inputs look bigger than they are.
- **Mock only true boundaries:** network, containers, GPU, very slow tools, the OS or platform, and the clock. Don't patch private helpers (`_name`) or the command runner to record what would have run.
- **Assert argv only where the flags are the scientific contract.** For example, a caller's ploidy or a minimum mapping quality. Assert just those flags, with a `# contract: <why>` comment.
- **Check message codes or key facts, not whole prose.** Help text is checked once, by the generated-help and doc tests.
- **Name the bug in each regression test** (`#123` or a short description), and check the symptom the user saw.
- **Don't restate constants, defaults or re-exports.**

`pixi run test-smells` lists private-internal patches and whole-argv asserts in test lines you added since `origin/main`. CI shows the same list as warnings on the PR. It is advisory and never fails.

## Make a change with an AI agent

The project is developed with AI coding agents. [AGENTS.md](../AGENTS.md) holds the rules they follow: test and run what they write, run ruff and mypy, keep outputs in `out/` or `tmp/`, keep defaults unchanged.

1. Install an agent, for example GitHub Copilot CLI (`npm install -g @github/copilot`), and start it in the repository: `copilot`.
2. Pick an issue from the [good first issue](https://github.com/WGSExtract/wgsextract-cli/labels/good%20first%20issue) list and tell the agent the outcome, how to check it, and where to stop. For example:

   > Fix issue #N. Add tests that fail without the change, run `just check`, then open a pull request that closes the issue.

3. Review what it did: read the diff (`/diff` in Copilot CLI, or `git diff`), and check that the tests would fail without the change. Run the command yourself on real data when you have it.
4. Have it push and open the pull request, then watch CI (`gh pr checks --watch`). Paste failures or review comments back to the agent until it's green.

Keep each change small and state how you'll verify it. Most problems show up when the result is checked against real data, not in review.

## Release a change

Merged changes ship in the next release; see [releasing.md](releasing.md).
