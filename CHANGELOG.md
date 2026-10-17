# Changelog

## Unreleased

**Fixed**
- **Edits to ignored secrets show in the evidence.** Snapshots honour `.gitignore`, so an executor's change to a gitignored file such as `.env` was invisible to the evidence. orch now fingerprints the sensitive files outside git before and after every attempt.
  - The fingerprints are a sha256 and size per file, kept in memory and never stored.
  - A change is recorded as a `policy.ignored_sensitive_changed` audit event, even for runs that fail.
  - It is listed in `orch review show`, and counts as a scope violation and a sensitive-file flag, so APPROVED is refused.
  - Changes you make yourself between runs are not attributed to the executor.

## 0.5.0 (2026-10-17)

Validated on a real repository (Phase 5): a large CodeIgniter and Vue platform whose PHP runs only in Docker, with no test suite. One real bug fix went through plan, dispatch, review, one correction and approval. See [docs/validation-phase5.md](docs/validation-phase5.md).

**Fixed** (found by the run)
- **Default branches are protected.** A project's own default branch counts as protected, whatever it is called. A repository that integrates on `develop` was not covered by `protected_branches`, so `agy` would have edited `develop` directly. Work now moves to `orch/P<n>` first, and `orch task commit` refuses the default branch.
- **Discovery reads Make variables.** A Makefile target that only manages containers or images (`docker compose build`, `up`, `down`, ...) is no longer proposed as a check. `make build` wrapping `$(COMPOSE) build` had been proposed with high confidence as the build command. Discovery expands simple Make variables, so a recipe that reaches Docker through them is marked as needing services.
- **Secrets outside git are named.** `agy` can read any file in the working tree, including gitignored ones like a `.env` with live credentials, and orch said nothing about it.
  - `orch project engine --allow` now warns with the list of sensitive files outside git.
  - `orch project show` lists them.
  - Every dispatch to an executor that sends code off the machine records a `policy.secrets_readable` event and prints a warning.
- **Corrections show their own diff.** Evidence stores each attempt's patch, and `orch review show` shows "This attempt's diff" before the cumulative one. Before, a correction's review only showed the diff since the task started.
- **Commit messages follow the repository.** `orch task commit --dry-run` adds a note when recent subjects use Conventional Commits types (`fix:`, `feat:`) and the message doesn't. Its `To commit:` hint now repeats `--message-file`, which it used to drop.

**Changed**
- The `orchestrate` skill:
  - asks the user to move sensitive files out of the tree while tasks run;
  - reads the per-attempt diff when reviewing a correction;
  - writes commit messages in the repository's own style.

## 0.4.0 (2026-10-17)

Reliability and safety (Phase 4).

**Added**
- `orch task commit <task>...` commits approved tasks after your OK. Policy action `git_commit` needs approval by default, so the command is refused for Claude unless the policy makes it automatic.
  - It commits exactly the files the evidence attributes to the executor: never check artifacts, other uncommitted work, or what is already staged in your index.
  - Every file must still hold its reviewed content. A file changed again by a later approved task needs both tasks in one commit.
  - The message comes from the task and the engine's evidence, with `Orch-Task:` trailers. orch never adds `Co-Authored-By` trailers, and it strips them from messages it is given.
  - `--dry-run` shows the files and the message without committing. The command refuses protected branches, and never pushes.
- `orch doctor` shows the `agy` version, and warns when it differs from the version the adapter was verified against (1.4.0).
- `orch doctor --probe` runs one tiny read-only `agy` turn in an empty directory, to check that the account works. It classifies location, licence, eligibility, sign-in and rate-limit errors, and never runs without the flag.
- [docs/agy-command-grants.md](docs/agy-command-grants.md): what `agy`'s scoped command grants are, what was verified, and why orch doesn't use them yet.

**Fixed** (found by the new failure-injection tests)
- **Recovering a killed engine.** After `orch recover`, `orch task retry` could not continue when the killed run had already edited files: preflight blocked the retry on the executor's own unfinished work. `orch recover` now stops the executor, waits for it to exit, and records where the run stopped. The retry continues from there, and its evidence includes those edits.
- **Edits from failed runs.** Edits made by a run that then failed (a rate limit, for example) were dropped from the evidence when the retry didn't touch those files again. Every run now records its end state, and evidence covers the edits of all of a task's runs.
- **Corrupted executor output.** A corrupted `agy` stream could crash the adapter and leave the task RUNNING: an event of the wrong shape, a non-text response, `NaN` token counts. The adapter now skips and counts unreadable lines, and reports a classified executor error. If an adapter raises anyway, the engine records the run as FAILED.

**Tests**
- Failure injection:
  - `orch run` killed with SIGKILL mid-run, then `orch recover`, `--kill` and `orch task retry`;
  - cancelling a real executor subprocess from another connection;
  - transient-retry backoff exhaustion;
  - corrupted, truncated and mistyped `agy` streams.
- Tests no longer read your global or system git configuration.

## 0.3.1 (2026-10-17)

**Fixed**
- Files created by the engine's own check runs (for example Python's `__pycache__/`, logs, coverage output) were attributed to the executor. They could show up as scope violations, block corrections, and block dependent tasks. Evidence now charges the executor only with what changed during its own attempts. The engine snapshots again after the checks, and lists those files separately as check artifacts.
- The tests now run with bytecode writing enabled, as on a fresh CI runner, which is how this bug surfaced.

**Changed**
- CI uses `actions/checkout@v5` and `actions/setup-python@v6` (Node 24).

## 0.3.0 (2026-10-17)

The first end-to-end release: Claude plans and reviews, Antigravity implements, orch referees.

**Execution**
- `agy` adapter: streaming progress, schema-validated reports, conversation resume, process-group cancellation.
- Failure classification: rate limits are retried with backoff; location, licence and eligibility errors escalate.
- Manual fallback executor: a paste-ready prompt for the Antigravity desktop app, plus a report file.
- Scheduler with global and per-repository concurrency limits; `orch recover` for runs left by a crashed engine.

**Evidence and review**
- Working-tree snapshots through a temporary git index; exact per-task and per-attempt diffs.
- Scope violations, and policy flags on the files that actually changed.
- Confirmed checks run by the engine, with secrets redacted from their output.
- Auto-fix loop for failing checks, with no Claude tokens spent.
- `orch review show` / `orch review decide`. `APPROVED` is refused when the evidence doesn't support it.
- Corrections resume the executor's conversation with the original task and every earlier finding.

**Usability**
- Per-run token usage, including executors that report cumulative totals.
- Dependent tasks can build on approved, uncommitted work.
- `orch skill install` installs the bundled `orchestrate` Claude Code skill.

**Validation**
- Validated live with `agy` on a sample repository ([docs/validation-phase3.md](docs/validation-phase3.md)).

## 0.2.0

Core engine:
- Project registry and stack discovery.
- Config and approval policy.
- Plan import with validation.
- Persistent task state machine with an append-only audit log.
- Layered context with provenance.
- Executor interface.
- CLI.

## 0.1.0

Architecture proposal and verified integration assessment ([docs/architecture.md](docs/architecture.md)).
