# CI PR Autopilot

## Purpose

Use this skill when the user wants an autonomous PR babysitter: watch GitHub CI and review feedback, make sensible fixes, reply when feedback should not be applied, push follow-up commits, resolve handled threads, rerun checks, and merge only when the PR is clean.

The loop is intentionally conservative. Prefer stopping and reporting over guessing on risky, destructive, security-sensitive, or product-significant changes.

## Requirements

- Work inside the PR branch checkout.
- Use `gh` for all GitHub operations.
- Use non-interactive git commands only.
- Do not force-push, rebase, amend, delete branches, change repository settings, bypass protections, or skip hooks unless the user explicitly asked for that exact action.
- Do not merge with failing required checks, unresolved material review threads, requested changes, merge conflicts, missing approvals required by branch protection, or uncommitted local changes.
- Do not resolve or close a thread unless the latest pushed commit directly addresses it or you have replied explaining why it is not being changed.

## Initial Discovery

1. Identify the PR and repository:
   - If the user gave a PR URL, use `gh pr view <url> --json ...`.
   - Otherwise use `gh pr view --json ...` from the current branch.
2. Capture baseline state:
   - `git status --short --branch`
   - `git remote -v`
   - `gh pr view --json number,title,url,headRefName,baseRefName,mergeStateStatus,reviewDecision,isDraft,maintainerCanModify,commits,statusCheckRollup,latestReviews`
   - `gh pr checks --watch=false` or `gh pr checks <number> --watch=false`
   - `gh api repos/{owner}/{repo}/pulls/{number}/comments`
   - `gh api repos/{owner}/{repo}/pulls/{number}/reviews`
   - `gh api repos/{owner}/{repo}/pulls/{number}/commits`
   - `gh api graphql` for review threads, including thread id, resolved state, path, line, original line, comments, author, body, and diff hunk.
3. Determine the repo's local verification commands before changing code:
   - Prefer project docs, package scripts, CI config, Makefile, task files, or existing contributor docs.
   - Build a comprehensive local test command set covering formatting, linting, typecheck, unit tests, integration tests, and build when available.
4. Determine the commit identity before creating any commit:
   - If the repo has a `.dev_id` file or documented developer identity file, use the matching name and email from it.
   - Otherwise use the GitHub user that checked out or owns the current PR branch when that can be determined from `gh auth status`, `gh api user`, the remote URL, and PR metadata.
   - If multiple GitHub users are authenticated or configured, match the commit name and email to the repository owner when that owner appears in the local owner/user list.
   - Do not change global git config. Prefer explicit per-commit identity, such as `git -c user.name=... -c user.email=... commit ...`, or repo-local config only when the repo already uses that convention.

## Review Comment Policy

Classify every unresolved PR thread and actionable review comment.

Apply the feedback when:

- It identifies a real bug, missed test, clarity issue, maintainability issue, performance issue, accessibility issue, security hardening, or repo convention mismatch.
- The requested change is small enough to implement safely with the visible context.
- It does not conflict with explicit user requirements or branch purpose.

Refuse the feedback when:

- It is incorrect, obsolete, already addressed, conflicts with requirements, would add unnecessary scope, would create a regression, or needs a product decision.
- The requested change is ambiguous enough that implementing it would be a guess.

When refusing, reply directly in the PR thread or review comment with exactly this prefix:

```text
(AI): Reason: <concise factual explanation>
```

Keep refusal replies short and specific. Do not be defensive.

When a comment requires a fix, do not reply with intent only. Make the fix, push it, then resolve the thread after the fix is present on GitHub. Optionally reply with a short note if the reason is not obvious.

## CI Failure Policy

For every failing check:

1. Inspect the failing job details with `gh run view`, `gh run download`, or provider-specific logs exposed through `gh`.
2. Reproduce locally when feasible.
3. Fix root causes, not symptoms.
4. Run the narrowest relevant local verification first, then the comprehensive local suite before merge.
5. Push a normal commit.
6. Wait for CI to rerun and finish.

Stop and report instead of continuing when:

- CI failure is caused by an external outage, expired credential, missing secret, quota issue, or flaky infrastructure that cannot be fixed in code.
- The fix requires secret values, production credentials, repository settings, paid services, or access you do not have.
- The failure suggests a broad design problem or product decision outside the PR scope.

## Editing And Commit Rules

- Before editing, inspect relevant files and current diffs so you do not overwrite user or other-agent changes.
- Keep changes minimal and directly tied to a CI failure or review comment.
- Do not include unrelated cleanup.
- Run formatting only when it is part of the repo workflow or necessary for changed files.
- Commit only your changes, with a clear message describing the fix.
- Commit using the identity selected during initial discovery. The commit author should match `.dev_id` when present, otherwise the GitHub user associated with this checkout/PR. If multiple GitHub users are available, prefer the identity whose user/email matches the repository owner when that owner is in the known owner list.
- Push the branch after each coherent fix batch.
- If hooks modify files during commit, inspect the result and create a new commit for remaining changes rather than amending unless the user explicitly requested amend.

## Resolving Threads

Use GitHub GraphQL for review threads because REST does not fully manage thread resolution.

Useful queries and mutations:

```sh
gh api graphql -f query='query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      reviewThreads(first:100) {
        nodes {
          id
          isResolved
          path
          line
          originalLine
          comments(first:50) {
            nodes { id author { login } body url createdAt }
          }
        }
      }
    }
  }
}' -F owner=OWNER -F repo=REPO -F number=NUMBER
```

```sh
gh api graphql -f query='mutation($threadId:ID!) { resolveReviewThread(input:{threadId:$threadId}) { thread { id isResolved } } }' -F threadId=THREAD_ID
```

```sh
gh api graphql -f query='mutation($subjectId:ID!, $body:String!) { addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$subjectId, body:$body}) { comment { url } } }' -F subjectId=THREAD_ID -f body='(AI): Reason: ...'
```

Only resolve a thread after one of these is true:

- The fix was pushed and the thread's concern is objectively addressed.
- You replied with `(AI): Reason: ...` explaining why no change will be made.
- The thread is obsolete because the commented code no longer exists and the concern no longer applies.

## Monitoring Loop

Repeat until complete or blocked:

1. Refresh PR state, review threads, reviews, commits, mergeability, and checks.
2. If new failing CI exists, diagnose, fix, test, commit, push, and wait.
3. If new actionable review feedback exists, fix or refuse with `(AI): Reason: ...`, commit, push if changed, and resolve handled threads.
4. If changes were pushed, wait for new CI results before merging.
5. If CI is pending, wait and poll at a reasonable interval. Avoid tight loops.
6. If PR state changes while you are working, re-read state before resolving threads or merging.
7. Continue until no failing/pending required checks, no unresolved material threads, no unhandled requested changes, and no new review comments remain.

Treat these as serious blockers and stop with a clear report:

- Merge conflicts.
- Draft PR unless user explicitly says to mark ready.
- Required approval missing or latest review decision is `CHANGES_REQUESTED` after all sensible fixes/refusals.
- Required CI is unavailable or permanently failing for non-code reasons.
- Local comprehensive tests fail for a reason you cannot fix safely.
- A requested fix would require changing architecture, public API, schema, migrations, security boundaries, or product behavior beyond the PR's scope.

## Local Comprehensive Testing

Before merging, run the repo's comprehensive local checks. Common examples:

- `npm test`, `npm run lint`, `npm run typecheck`, `npm run build`
- `pnpm test`, `pnpm lint`, `pnpm typecheck`, `pnpm build`
- `bun test`, `bun run lint`, `bun run typecheck`, `bun run build`
- `pytest`, `ruff check`, `mypy`, package build commands
- `go test ./...`, `go vet ./...`
- `cargo test`, `cargo clippy -- -D warnings`
- `make test`, `make lint`, `make build`

Use the commands that actually exist for the repo. Do not invent scripts. If the comprehensive suite is too slow or requires unavailable services, stop and report that limitation unless the user already authorized a narrower suite.

## Merge Policy

Only land the PR when all conditions are true:

- Working tree is clean.
- Branch is pushed and GitHub shows the latest head SHA.
- Required CI checks passed on the latest commit.
- No required or material checks are pending.
- No unresolved actionable review threads remain.
- No latest `CHANGES_REQUESTED` review remains unaddressed.
- Required approvals are present.
- Local comprehensive testing passed after the final changes.
- PR is not a draft.
- Mergeability is clean and branch protection allows merge.

Use the repository's normal merge method. If uncertain, inspect allowed merge methods or existing project convention. Prefer `gh pr merge --auto` when branch protection requires queued/auto merge, otherwise use the repo's standard method. Do not use admin override.

After merging:

- Check out the PR base branch, usually `main`, with `git checkout <baseRefName>`.
- Pull the latest changes for that branch with `git pull --ff-only`.
- If checkout or pull fails because of local changes, conflicts, or branch tracking issues, stop and report the blocker instead of forcing, stashing, resetting, or deleting anything.

Then report:

- PR URL.
- Final merge result.
- Commits pushed by you.
- CI status.
- Local comprehensive test commands run.
- Any comments refused with their `(AI): Reason: ...` summaries.
