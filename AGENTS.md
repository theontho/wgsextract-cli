We do not want to manage installing dependencies for users, just validate that they are installed before running the feature. You can add library dependencies as part of the pyproject.toml / pixi.toml file.

In general, test and run the code you write.  You are not done if all you do is edit code.  Make sure to test small test genome extraction versions and then full genome versions once the small extraction works. Issues often pop up in full genome runs that don't show up in small test data runs.

You have a large budget and a lot of time to do things correctly.  Favor 'the best practice' or 'correct' way over trying to achieve a solution quickly.  This does not mean doing something in an overly verbose way is "correct", conciseness is it's own virtue.

When outputting test run results, logs, or stdout captures, **always** put them in a gitignored `out/` or `tmp/` directory (or a subdirectory within them). Never output these files to the repository root.  Make scratch or temp scripts in the `scratch/` dir.

Do not add new environment-variable configuration. Put persistent configuration in config.toml settings, and use command-line parameters for run-specific overrides.

## Python Formatting & Linting

- **Always** run `pixi run ruff check --fix {file_path}` and `pixi run ruff format {file_path}` after editing a Python file in this directory.
- **Always** run `pixi run mypy {file_path}` (or `pixi run mypy src/wgsextract_cli`) to check for type errors before concluding a task.
- Run these commands from the repository root. Dev tools (ruff, mypy, pytest, pre-commit) live in the `dev` Pixi environment; `pixi run <tool>` selects it automatically.

## Babysitting a PR

"Babysit" a PR (or "babysit landing" it) means: shepherd it until it merges, using the `ci-pr-autopilot` skill (`.agents/skills/ci-pr-autopilot`).

- Fix review comments that are legitimate, then resolve their threads. Reply `(AI): Reason: ...` to the ones you decline, then resolve those too.
- Fix real issues that CI finds, or fix CI itself when the failure is in CI rather than in the code.
- Push follow-up commits and wait for CI to rerun.
- Once the PR is in a passable state, merge it. That means CI is green, no material threads are unresolved, and the local checks pass.
