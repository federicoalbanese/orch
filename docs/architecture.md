# orch architecture

Status: Phases 1 to 5 are implemented. Phase 3 was validated live with `agy` on a sample repository ([validation-phase3.md](validation-phase3.md)), and Phase 5 on a real production repository ([validation-phase5.md](validation-phase5.md)). Phase 6 is planned.

Every capability claim below was verified by probing the real tools (section 2), not assumed.

orch is a universal engineering orchestration framework:

- **Claude** (Claude Code) understands, plans, decides, and reviews.
- **Antigravity** (the `agy` CLI) implements.
- A **deterministic engine** (`orch`) keeps state, enforces policy, dispatches work, and collects evidence, so that neither model has to be trusted for bookkeeping or safety.

---

## 1. Design inputs

### Reference environment

| Item | Version used | Consequence |
|---|---|---|
| Antigravity CLI | `agy` 1.4.0, self-updating | The only automatable Antigravity interface (section 2) |
| Antigravity desktop | 2.x | No public API; used only as the manual fallback |
| Claude Code | 2.1.x, interactive and `claude -p` headless | The brain. Interactive first; headless later for unattended runs |
| Python | 3.11 to 3.14, sqlite 3.35+ | Engine written in **stdlib-only Python**, so there is nothing to install |
| git | 2.x | Diff, snapshot, and branch operations |
| Docker | optional | Many projects run their tests inside containers |

### What real repositories look like

The design was checked against a mixed set of real-world repositories. They included:
- Laravel apps running in Docker (`artisan`, `phpunit.xml`, `docker-compose.yml`, `Makefile`),
- Node frontends,
- Go services,
- Python packages,
- Flutter apps,

all hosted on self-hosted GitLab rather than GitHub. Three facts follow:

- **Discovery has to detect the stack rather than assume it.**
- **Test commands differ per project** (`make test`, `docker compose exec -T app php artisan test`, `go test ./...`, `pytest`). The toolchain often exists only inside a container, never on the host.
- **Commands must be confirmed by a person** before they are trusted, and Git hosting can't be assumed to be GitHub.

---

## 2. Integration assessment (verified by probing)

### 2.1 `agy` (Antigravity CLI)

| Capability | Status | Evidence |
|---|---|---|
| Headless run: `agy -p`, `--output-format json` | ✅ Verified | One JSON object with `status`, `response`, `conversation_id`, `usage`, `denied_actions` |
| Live progress: `--output-format stream-json` | ✅ Verified | NDJSON: `init` (cwd, tool list), many `step_update` (step index/type/state/text delta), `result` |
| Structured report: `--json-schema` | ✅ Verified | Returns a schema-conforming `structured_output` object. This is the executor's report contract |
| Read and edit files: `--mode accept-edits` | ✅ Verified | Fixed a planted bug; added functions; diffs matched its claims |
| Resume and correct: `--conversation <id>` | ✅ Verified | Remembered its previous turn. This is the backbone of the correction loop |
| Parallel runs | ✅ Verified | 2 concurrent runs both succeeded |
| Cancellation (SIGTERM) | ✅ Verified | Exit code 1, `error: interrupted`, no orphan processes, no partial edits |
| Usage metering | ✅ Verified | Input, output, thinking, and cache-read tokens, as a **running total per conversation**. **No currency cost** is reported |
| Audit trail | ✅ Verified | Full tool-call transcript at `~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/logs/transcript.jsonl` |
| Model choice | ✅ Verified | `--model`. `agy models` lists the models available to the account |
| Read-only config queries | ✅ Verified | `agy -p "/permissions"` answers from configuration: no agent turn, no quota, no conversation |
| Run shell commands (tests, builds) headless | ❌ **Blocked without a grant** | Auto-denied (`denied_actions: RunCommand`) in every mode tried, unless an allow-rule covers the command (scoped allow-rules, below) |
| `--mode auto` | ❌ Unavailable | "not available for this account" on the account tested |
| `--sandbox` | ❌ No effect | Commands still denied |
| Scoped command allow-rules | ✅ **Verified headless** (live test, 2026-10-17, `agy` 1.4.0) | A global `command(<prefix>)` rule in `permissions.allow` (`settings.json`) let a headless `request-review` run execute the granted command. A command with no rule was still denied. Rules match by prefix, outlive tasks and bypass `agy`'s sandbox, so orch doesn't use them. The project-scope format is still unconfirmed. Findings: [agy-command-grants.md](agy-command-grants.md) |
| `--dangerously-skip-permissions` | 🚫 Rejected by policy | Grants everything; incompatible with least privilege |
| Desktop app API | ❌ None found | Not used |

### 2.2 Operational caveats

- **Behaviour can change on update.** `agy` updates itself, so its CLI and output format can change between versions. The adapter isolates this, contract tests replay recorded output, and `orch doctor` warns when the installed version differs from the verified one (1.4.0).
- **Output can be corrupted or cut off.** The adapter skips and counts unreadable lines and events of the wrong shape, so a damaged stream ends as a classified executor error. If an adapter still raises, the engine records the run as FAILED instead of leaving the task RUNNING.
- **A timeout looks like success.** When `--print-timeout` fires, `agy` still reports `status: SUCCESS` with an empty response, and only stderr says "print timeout". The adapter classifies this as a timeout.
- **Some errors come from the account, not the run.** `400` (location), `403` (licence), `429` (quota) and "not eligible" come from Google's side. Only `429` is retryable, with backoff. `orch doctor --probe` runs one tiny read-only turn in an empty directory and classifies these errors, so an account problem shows up before a task fails on it. It sends a request to Google, so it only runs when asked.
- **Latency varies widely.** A trivial resumed turn took 60 s, while a fresh one took 3 s. Timeouts must be generous and progress has to come from streaming.
- **Reports use absolute paths** and `file://` markdown links, so the engine normalizes them.
- **Code goes to Google.** Every task sends source code to Google's servers. Employer or client repositories may forbid that, so projects are **opt-in** for external execution (R3).
- **`agy` reads more than tracked code.** A task's scope limits what `agy` may change, not what it may read, and gitignored files such as `.env` are in the working tree too. orch names the sensitive ones at opt-in, in `orch project show`, and at every dispatch (R3).
- **Snapshots can't see ignored files.** So orch also fingerprints the gitignored sensitive files around every attempt, in memory only (sha256 and size, never stored). A change is recorded as an audit event. In the evidence it is a scope violation when outside the task's scope, and always a `secrets_change` flag, which can never be automatic. A directory with more than 2,000 files is listed as unchecked instead.

### 2.3 Claude side

- **Interactive Claude Code is the primary brain.** Approvals happen naturally in the conversation, and it needs no extra API key.
- **Headless `claude -p` is available for later** (Phase 6) for unattended planning and review. It supports `--json-schema`, `--max-budget-usd`, `--resume`, `--permission-mode`, and `--allowedTools`. Each run reports its actual cost, so the engine can record real spend instead of estimates.

### 2.4 The decision these facts force

`agy` cannot run tests or builds unattended. So **the engine runs every verification command itself**, and feeds failures back to `agy` through `--conversation`. The review rules require this anyway: test evidence must come from a run the engine captured, not from the executor's claim. Scoped grants do work headless, so letting `agy` run its own commands could become an opt-in capability. orch doesn't offer it yet.

---

## 3. Architecture

### 3.1 Components

```text
                ┌───────────────────────────────────────────────────┐
  You  ───────▶ │  Claude Code (brain)                              │
  goals,        │  skill: orchestrate  ·  plans · decides · reviews │
  approvals     └──────────────┬──────────────────────▲─────────────┘
                               │ orch CLI (JSON in/out)│ evidence bundles
                ┌──────────────▼──────────────────────┴─────────────┐
                │  orch engine (Python stdlib, UI-independent)      │
                │                                                   │
                │  registry ─ discovery     planner-contract (plan  │
                │  context (layered)        validation, DAG, scope) │
                │  policy (risk, approvals) scheduler/dispatcher    │
                │  evidence (git, verify)   review + correction     │
                │  git safety               audit/event log         │
                │                                                   │
                │  store: SQLite (WAL) + per-project knowledge dir  │
                └──────────────┬──────────────────────▲─────────────┘
                               │ ExecutionEngine interface
          ┌────────────────────┼──────────────────────┼────────────┐
          ▼                    ▼                      ▼            │
   AgyAdapter            ManualAdapter           FakeAdapter       │
   agy -p, stream-json,  writes a paste-ready    deterministic,    │
   --json-schema,        task file for the       for tests         │
   --conversation        Antigravity desktop,                      │
                         collects result + diff                    │
          │                                                        │
          ▼                                                        │
   your repositories  ◀── verification commands from project config┘
```

### 3.2 Responsibilities

| Layer | Owns | Never does |
|---|---|---|
| Claude (skill) | Understanding, inspection, architecture, task breakdown, acceptance criteria, review decisions, correction instructions, decision records | Bookkeeping, running tests by hand, trusting the executor's claims |
| Engine | State machine, persistence, policy enforcement, scheduling, dispatch, snapshots, diffs, scope checks, verification runs, retries, audit log, redaction | Making design decisions; running commands that came from model output |
| Executor (`agy`) | Implementing a task within its declared scope; reporting through the schema | Redefining scope or architecture; running commands (orch grants it none) |

### 3.3 Data contracts (JSON Schema, versioned)

`orch schema plan|report|review` prints the exact contracts.

- **Project:** `id`, name, description, root path, git remote, default branch, languages and frameworks (detected), `commands` (`test`, `lint`, `build`, `typecheck`, ..., each with cwd, timeout, whether it needs services, and whether a person confirmed it), `allow_external_engine` (opt-in), executor and model preferences, policy overrides.
- **Plan:** objective, context summary, decision plus rationale and trade-offs (flagged when architectural), risks, unknowns, tasks.
- **Task:** `key`, objective, `scope.allow` and `scope.deny` path globs, relevant files, instructions, `acceptance[]` (each with `id`, text, and `verify` as a command or `review`), required checks, `risk` (low/medium/high), `depends_on[]`, deliverables.
- **Run:** task, attempt, kind (`execute`, `correction`, `auto_fix`, `retry`), executor, conversation id, timing, status and classification, usage, transcript path, before and after tree ids.
- **Evidence:** the diff since the task started and the diff of this attempt (both shown in the review bundle when they differ), changed files, scope violations, policy flags, results of each verification command (exit code, duration, truncated and redacted output), the executor's report, and discrepancies between the claimed and the actual diff.
- **Review:** decision (`APPROVED`, `NEEDS_CORRECTION`, `BLOCKED`, `NEEDS_HUMAN_APPROVAL`), every acceptance criterion with `met` and evidence, and findings (severity, category, file and line, message, actionable instruction).

### 3.4 Task state machine

```text
PLANNED → AWAITING_APPROVAL ─→ READY
READY → RUNNING → EXECUTED → VERIFYING ─(auto-fix ≤ N)→ READY …
VERIFYING → IN_REVIEW
IN_REVIEW → APPROVED
          → NEEDS_CORRECTION → READY (same conversation, all prior findings attached)
          → BLOCKED | NEEDS_HUMAN_APPROVAL
any active state → CANCELLED | FAILED (retry/budget exhausted → escalate)
RUNNING with a dead engine → INTERRUPTED → READY (orch recover, orch task retry)
```

- **Recovery keeps attribution.** `orch recover` snapshots the working tree once the executor is gone, and records it as where the dead run stopped. The same happens for every run that fails. A retry then treats that run's unfinished edits as the executor's own: they don't block the retry, and its evidence includes them. Nothing is reverted.

- **State changes are checked.** Every change is validated against this table, carries a reason, and is written to an append-only audit log in the same transaction.
- **No duplicate dispatch.** Status changes are compare-and-set, and each run has a unique `(task, attempt, kind)` key.
- **No rerunning approved work.** An `APPROVED` task is never re-dispatched unless it is explicitly reopened.

### 3.5 The correction loop

1. **Dispatch.** The engine sends the task to the executor with its scope, criteria, and report schema, then snapshots, diffs, and scope-checks the result.
2. **Auto-fix loop (deterministic, cheap).** If a confirmed verification command fails, the engine resumes the same conversation with the failing output, up to `auto_fix_attempts` (default 2). No Claude tokens are spent.
3. **Claude review (mandatory).** The engine renders a compact evidence bundle. Claude judges it against:
   - the acceptance criteria,
   - architecture,
   - security and error handling,
   - backward compatibility and regressions,
   - test coverage and the actual results,
   - unrelated changes.

   Passing tests alone are never treated as sufficient.
4. **Corrections.** A `NEEDS_CORRECTION` decision resumes the conversation with the original task, every earlier finding, and the new instructions.
5. **Limits.** `max_review_cycles` (default 3) stops the loop and escalates to the user instead of looping. Failures are classified as transient, environment, permission, executor error, timeout, or cancelled.

### 3.6 Layered context, loaded per task

1. **Global framework rules:** the executor rules and the skill.
2. **Project context:** `~/.local/share/orch/projects/<id>/context.md`, written by people and Claude.
3. **Project knowledge entries:** each with a source, the commit it was verified at, and the paths it depends on.
4. **Task:** the spec, plus the relevant files only.
5. **History:** this task's runs and findings.

Rules:
- **The context budget is enforced.** Layers are trimmed to `limits.context_budget_chars`, and the task layer is never cut.
- **Stale knowledge stops reaching prompts.** `orch context check` marks entries stale when the files they depend on change, and they stay out until someone re-verifies them.
- **Context never crosses projects.** Shared engineering knowledge is opt-in.
- **Inputs are untrusted.** Repository files, executor output, and logs are wrapped as data in evidence bundles. Policy lives in engine code and config, never in text a model could be talked out of.

### 3.7 Policy and approvals (configurable, with per-project overrides)

**Automatic by default:**
- Inspection and discovery.
- Edits inside the declared scope.
- Running the project's confirmed verification commands.
- Creating a working branch.

**Requires approval by default:**
- High-risk tasks and architectural decisions.
- Unbounded scope.
- Dependency or lockfile changes.
- Database migrations.
- Files matching sensitive globs (`.env*`, keys, CI config, `infra/`, `deploy/`).
- File deletions and large deletions.
- Starting services.
- Commits, pushes, merge requests and merges.

**Never automatic, whatever the config says:**
- Enabling an external engine for a project.
- Destructive migrations.
- Secret or security-policy changes.
- Production infrastructure.
- Production deployment.
- Force-push.
- Merging into protected branches.
- Discarding uncommitted user changes.

**Two checks:** policy is checked at plan import (on what the plan declares) and again after execution (on the files that actually changed).

### 3.8 Git integration

- **Preflight.** A task does not start if uncommitted changes in its scope would be attributed to the executor. Changes that byte-match what an approved task produced don't count, so dependent tasks can build on approved work before it is committed. Later runs of the same task compare against its own last snapshot.
- **Snapshots.** The whole working tree is recorded through a temporary git index (`read-tree`, `add -A`, `write-tree`). The user's index, files and branches are never touched.
- **Branching.** Work that would start on a protected branch moves first to `orch/P<n>` (`git switch -c`, which keeps uncommitted work). A project's own default branch always counts as protected, whatever it is called, so a repository that integrates on `develop` is covered too.
- **Commits.** `orch task commit <task>...` commits approved tasks after the user's OK (policy action `git_commit`; refused for Claude unless the policy makes it automatic). `--dry-run` shows the files and the message without committing.
  - **Exactly the task's files.** It commits the files the evidence attributes to the executor, never check artifacts, other uncommitted work, or anything already staged in the user's index (`git commit --only`).
  - **Only reviewed content.** Each file must still hold its approved content. A file changed again by a later approved task needs both tasks in one commit.
  - **Literal paths.** File names are never treated as patterns.
  - **Protected branches.** Committing on one is refused.
  - **The message.** It is built from the task spec and the engine's evidence, with `Orch-Task:` trailers, or taken from `--message-file`. When the repository's recent subjects use Conventional Commits types and the message doesn't, the plan carries a note. orch never adds `Co-Authored-By` trailers, and it removes any from a message it is given.
  - **Never pushes.** Pushing, MRs and merges stay with the user.
- **Concurrency.** One task per repository working tree by default. Parallelism happens across repositories.

### 3.9 Cost and usage

- **Executor usage:** actual token usage is recorded per run. For executors that report running totals per conversation (as `agy` does), the engine stores each run's own increment.
- **Claude usage:** recorded when headless `claude -p` is used. Interactive usage is not guessed.
- **No LLM where a tool will do:** formatting, linting, tests, schema validation, and scope checks are all deterministic.

### 3.10 Interface

- **`orch` CLI:** JSON output via `--json` for Claude, human tables otherwise.
- **First UI:** the bundled Claude Code skill `orchestrate`. The user states a goal, and Claude drives the CLI.
- **Later:** a dashboard can read the same SQLite store without changing the engine.

---

## 4. Roadmap

| Phase | Deliverables | State |
|---|---|---|
| 1 Discovery | This document | ✅ |
| 2 Core | Schemas and models, SQLite store with migrations, config and policy, project registry and stack discovery, executor interface, CLI, unit tests | ✅ |
| 3 First end-to-end | `agy` adapter, evidence collector, review and correction loop, auto-fix, scheduler, cancellation, recovery, manual fallback, Claude Code skill; validated live | ✅ |
| 4 Reliability and safety | Deeper `orch doctor` (version check, `--probe` account check), commits with approval, failure-injection tests (killed engine, real cancellation, retry exhaustion, corrupted output), scoped `agy` command grants evaluated ([findings](agy-command-grants.md)) | ✅ |
| 5 Real repositories | A live run on a real production repository (CodeIgniter and Vue, PHP only in Docker, no test suite), opted in deliberately; six defects fixed ([validation](validation-phase5.md)). Go, Python and Node remain to be validated live | ✅ |
| 6 Optimization | Headless `claude -p` mode, worktree parallelism, context compaction, usage reports, dashboard | Planned |

---

## 5. Risk register

| # | Risk | Mitigation |
|---|---|---|
| R1 | An `agy` self-update changes its CLI or output | The adapter isolates it and parses defensively; contract tests replay recorded and corrupted output; `orch doctor` warns when the version differs from the verified one |
| R2 | The Google account is rate-limited, unlicensed, or not eligible (429/403/400) | Back off on 429; classify the others as `ENV_BLOCKED` and escalate; nothing is half-applied; `orch doctor --probe` checks the account on request |
| R3 | **Employer or client source code is sent to Google** | `allow_external_engine` is off by default per project; only a person can opt a project in. Sensitive files outside git, which `agy` can still read, are named at opt-in and at every dispatch |
| R4 | `agy` can't run tests itself, so it iterates more slowly | Engine-run verification plus the auto-fix loop. Scoped grants work headless, but they are prefix-wide, outlive tasks and bypass `agy`'s sandbox, so orch does not use them ([findings](agy-command-grants.md)) |
| R5 | Docker-based tests need running containers and are slow | Per-command `requires_services` and generous timeouts; starting services needs approval |
| R6 | The executor edits outside scope or refactors unrelated code | Snapshot plus scope check: violations block approval and auto-fix. The engine never reverts work automatically |
| R7 | Concurrent tasks collide in one working tree | One task per repository by default; scope-overlap checks |
| R8 | Prompt injection through repository files or executor output | Evidence rendered as data; policy enforced in code; commands only from person-confirmed config |
| R9 | Endless correction loops or runaway usage | Cycle limits, failure classification, escalation |
| R10 | An agent records itself as the user | `ORCH_ACTOR` set in the agent's environment cannot be overridden by `--actor`; user-only actions are refused |
