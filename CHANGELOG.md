# Changelog

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
