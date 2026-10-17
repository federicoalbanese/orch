# Phase 5 validation: a real repository

Date: 2026-10-17. Executor: the real `agy` 1.4.0 (Gemini 3.8 Flash High). Engine: orch 0.4.0 plus the fixes below, released as 0.5.0.

Phase 3 ran on a sample repository written for the test. Phase 5 ran the same loop on a real one: a large, long-lived production codebase that orch had never seen.

## Choosing the repository

The user picked the repository. Every candidate was employer or client code, so each choice was weighed against what opting in means: `agy` sends the repository's code to Google.

- **Not used: the Python candidate** (`qc-pricing`, no remote). It had a 37-file refactor that wasn't committed. orch's preflight blocks any task whose scope overlaps uncommitted changes, so a real task there would have collided with the user's own work.
- **Chosen: Ghasedak24**, the employer's main travel-booking platform. The user confirmed that the employer's policy allows sending its tracked code through `agy`. Its stack:
  - backend: CodeIgniter 2.2 (not Laravel), PHP 7.3, running only inside a Docker container (`app`);
  - frontend: Vue 3 and Vite, plus a legacy bundle;
  - size: 11,954 tracked files and 24,397 commits;
  - tests: no automated suite at all.
- **Not covered: the Go and Python stacks.** The user chose to validate on Ghasedak24 alone.

## Setup

| Step | Who | Result |
|---|---|---|
| `orch project discover`, `orch project add` | Claude | Detected PHP, JavaScript, Vue, Vite, composer, pnpm and the compose services. Proposed one command: `make build` (defect 2) |
| Commands | Claude proposed them, the user confirmed them | `lint`: `php -l` inside the `app` container, on the PHP files changed relative to `HEAD`. `bale-phone`: a behaviour check for this task (below) |
| Opt in to `agy` | User | `orch project engine ghasedak24 --allow` |
| Secrets | User | The gitignored `.env` held live credentials, and `agy` can read any file in the tree. The user moved it out of the repository for the runs (defect 3) |
| Branch | Engine | Work moved from `develop` to a new local branch `orch/P1` before `agy` started (defect 1) |

The user-only steps worked as intended:

- Claude never attempted them.
- The user's first two reports that they were done didn't match orch's state: the audit log had no `project.engine` event, and `.env` hadn't moved. Claude checked the state before dispatching, and asked again.
- Dispatch would have been refused anyway (`external_engine_not_allowed`, `command_unconfirmed`).

## The task

**`BaleSafirApi::normalizePhone()`** builds the `phone_number` of every request to the Bale Safir messenger. It stripped non-digits, then turned a leading `0` into `98`. Three real inputs came out wrong:

| Input | Before | Expected |
|---|---|---|
| `00989121234567` | `980989121234567` | `989121234567` |
| `9121234567` | `9121234567` (no country code) | `989121234567` |
| `۰۹۱۲۱۲۳۴۵۶۷` (Persian digits) | empty string: the regex has no `/u` flag, so it strips them | `989121234567` |

The repository's AI-docs also claimed that `+98` numbers weren't handled. They were.

**The behaviour check.** The repository has no test suite, so the check is a PHP script in orch's data directory, outside the repository. It is piped into the `app` container, and calls the private method through reflection on 12 inputs: the 6 above plus 6 that already worked. Before the change it passed 6 of 12, failing exactly the defect cases.

**Plan P1** had one low-risk task:
- Scope: the class file and the AI-docs overview, with `.env`, `.env.*` and `.db/` denied.
- Required checks: `lint` and `bale-phone`.
- Two acceptance criteria verified by review: only the method body changes, and the doc states the new rules.

## What happened

| Run | Kind | `agy` time | Tokens in / out / thinking (per run) | Cache read | Engine checks | Decision |
|---|---|---|---|---|---|---|
| 1 | execute | 3m 01s | 63,831 / 28,851 / 27,233 | 315,739 | lint pass, bale-phone 12/12 | NEEDS_CORRECTION |
| 2 | correction | 1m 05s | 84,228 / 3,744 / 2,435 | 422,590 | lint pass, bale-phone 12/12 | APPROVED |

- **Totals:** 148,059 input and 32,595 output tokens for `agy`, plus 738,329 cache-read tokens.
- **Time:** from dispatch to approval, 6m 47s, including both reviews. The audit log holds 28 events, from registration to the knowledge entries.
- **Overhead:** orch's own time was about 10 s per run.
  - A working-tree snapshot of this repository takes 2.4 s, and a run takes three or four of them.
  - Both checks ran in 0.1 s each.
- **What `agy` touched:** its transcript shows it read and edited only the two files in scope.

### The reviews

**Review 1.** The change was correct. Both checks passed, every branch was traced by hand, the code is PHP 7.3-safe, and only the method body and the one doc item changed. Two minor maintainability findings were sent back anyway:

- **The branch order mattered, and nothing guarded it.** `0098` also starts with `0`, so swapping the first two branches silently brings back the `980989...` bug. The repository has no test that would catch that. The fix was one comment line.
- **The doc had lost its example.** The rewritten doc item no longer had the concrete before/after example the old one had.

**Review 2.** The correction added exactly one comment line and one example sentence, and nothing else. The bundle couldn't show that by itself (defect 4), so it was checked against the run's snapshot trees, and the task was approved.

**The commit.** It waits for the user's decision. The dry run committed exactly the two files, on `orch/P1`. orch's generated subject didn't follow the repository's `fix:` convention (defect 5), so Claude wrote a message in its style for `--message-file`. Nothing was pushed.

## Defects the run exposed, now fixed with tests

| # | Found | Problem | Fix |
|---|---|---|---|
| 1 | Setup | The repository integrates on `develop`, which no default `protected_branches` pattern names. `agy` would have edited `develop` directly, and `orch task commit` would have committed there | Each project's own default branch is protected, whatever it is called. Work moves to `orch/P<n>` before dispatch, and commits on the default branch are refused |
| 2 | Discovery | `make build` was proposed with high confidence as the build check. Its recipe is `$(COMPOSE) $(COMPOSE_FILES) build`, which rebuilds Docker images. Because the recipe used Make variables, discovery didn't even notice that it needs Docker | Discovery expands simple Make variables before reading recipes. A target that only manages containers or images (`build`, `up`, `down`, `pull`, ...) is not proposed as a check, and a warning says why. Targets that run something inside a container are still proposed, marked as needing services |
| 3 | Setup | `agy` can read any file in the working tree. The opt-in protects tracked code by consent, but orch said nothing about a gitignored `.env` full of live credentials | `orch project engine --allow` warns with the list of sensitive files outside git (matching `policy.sensitive_globs`), `orch project show` lists them, and every dispatch to an off-machine executor records a `policy.secrets_readable` event and prints a warning |
| 4 | Review 2 | Evidence recorded which files an attempt changed, but not its patch. A correction's bundle showed only the diff since the task started, so the reviewer couldn't see what the correction itself did | Evidence stores each attempt's own patch. The bundle shows "This attempt's diff" first whenever it differs from the cumulative diff |
| 5 | Commit | The generated subject (`Normalise 0098, ...`) ignored the repository's Conventional Commits convention (18 of the last 20 subjects start with `fix:` or `feat:`) | The commit plan carries a note when the recent subjects use Conventional Commits types and the message doesn't. The skill tells Claude to match the repository's convention with `--message-file` |
| 6 | Commit | The dry run's `To commit:` hint dropped `--message-file`, so copying it would have committed the generated message instead | The hint repeats `--message-file` with its absolute path |

The test suite went from 168 to 175 tests, with 1 skipped (the live `agy` test).

## Known limits, not fixed

- **Executor edits to ignored files are invisible.** Snapshots honour `.gitignore`, so evidence can't see if the executor changes a gitignored file such as `.env`. orch now names sensitive ignored files; detecting changes to them is a follow-up.
- **Ignored and untracked directories are checked as a whole.** The readable-secrets check sees them collapsed, the way git reports them. A `.env` inside an ignored directory isn't named.
- **The fallback default branch gets protected.** When a repository has no `origin/HEAD` and none of `main`, `master`, `develop` or `dev`, discovery takes the current branch as the default, and that branch becomes protected. `orch project set <id> default_branch <name>` corrects it.
- **Discovery is limited for PHP projects without tooling.** It doesn't detect CodeIgniter, and proposes no lint for a PHP project that has no linting tools. The changed-files `php -l` lint here was written by hand.
- **Only one stack and one task.** One repository and one task cover neither dependencies between tasks on a real repository, nor the Go, Python or Node stacks.
