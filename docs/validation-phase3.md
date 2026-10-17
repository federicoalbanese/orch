# Phase 3 validation: one full live cycle

Date: 2026-10-17. Executor: the real `agy` 1.4.0 (Gemini 3.8 Flash High). Engine: orch 0.3.0.

## Setup

- **Sample repository:** `pricing`, a tiny Python cart library written for this test. It contains no user code. It has a real bug: `Cart.total()` truncated instead of rounding, so 0.995 became 0.99. The commands are `make test` (pytest) and `make lint`.
- **Plan P1:**
  - **T1:** round totals half-up, plus regression tests.
  - **T2:** percentage discount codes. It depends on T1.
- **User-only steps:** the user confirmed the commands and allowed `agy` from their own terminal. Both commands had first been refused when attempted as Claude (exit code 5).

## What happened

| Run | Task | Kind | Duration | Tokens in / out (per run) | Engine checks | Decision |
|---|---|---|---|---|---|---|
| 1 | T1 | execute | 3m 14s | 108,682 / 24,044 | test 6 passed, lint ok | NEEDS_CORRECTION |
| 2 | T1 | correction | 0m 59s | 24,637 / 5,802 | test 8 passed, lint ok | APPROVED |
| 3 | T2 | execute | 3m 42s | 163,330 / 29,280 | test 15 passed, lint ok | NEEDS_CORRECTION |
| 4 | T2 | correction | 1m 36s | 103,683 / 8,777 | test 15 passed, lint ok | APPROVED |

**Totals:** about 400k input and 68k output tokens for `agy`, and about 13 minutes of wall time including reviews. The audit log holds 42 events.

### The reviews

**T1, review 1 (the planned forced cycle, on a real gap).** The fix was correct, but no test proved that rounding happens once, on the taxed total. An implementation that rounded before tax, or rounded each line, would have passed. I checked that the requested values tell those cases apart: 0.05 vs 0.06, and 0.01 vs 0.00.

The correction resumed the same `agy` conversation, added exactly the two tests, and left `cart.py` untouched. The per-attempt diff confirmed that.

**T2, review 1 (found in review, not forced).** The behaviour was correct. I verified it independently: SAVE20 on 59.97 at 8% tax gives 51.81. But all four changed files gained a trailing blank line, and a library-wide README sentence had landed under the new "Discount codes" heading.

The correction fixed exactly those. Its attempt diff was 2 lines added and 6 removed, all whitespace and the moved sentence.

## Defects the live run exposed, now fixed with tests

| # | Problem | Fix |
|---|---|---|
| 1 | `agy -p "<prompt>"` with stdin hangs, and `--print-timeout` still reports `status: SUCCESS` with an empty response | The adapter passes the prompt as an argument, and classifies a "print timeout" on stderr as TIMED_OUT, whatever the status says |
| 2 | A correction run was blocked by the executor's own earlier edits | After a task's first run, preflight compares against that task's last recorded snapshot |
| 3 | A dependent task (T2) was blocked by its approved, not-yet-committed dependency (T1) | In-scope uncommitted changes are accepted when they byte-match what an APPROVED task produced; any other edit still blocks |
| 4 | `agy` reports token usage cumulatively per conversation (verified: 31, 62, 93 output tokens over three identical turns), so summing runs overcounted corrections | Executors declare `cumulative_usage`; the engine stores each run's increment, and keeps the raw figure in evidence as `usage_reported` |

Runs 2 and 4 in the table use the corrected per-run figures. The validation database itself still holds the raw cumulative values, because it was recorded before fix 4.

## Not covered by this run

- **Real repositories.** No real repository was used; that is Phase 5.
- **Several agy failure modes.** Cancelling a live `agy` run, and real rate-limit, location or eligibility errors, were not triggered live. They are covered by adapter tests replaying recorded and constructed output, and cancellation was verified live during Phase 1 probing.
- **Manual executor.** The manual fallback has unit tests only.
- **Commits.** Git commits were out of scope. The work sits uncommitted on branch `orch/P1` in the sample repository.
