# Scoped `agy` command grants: findings

Date: 2026-10-17. Tool: `agy` 1.4.0 on Linux. Context: Phase 4, risk R4 ([architecture.md](architecture.md)).

**Question.** Headless `agy -p` denies every shell command (`denied_actions: RunCommand`), so it can't run tests itself. `agy` documents allow-rules for commands. Could orch grant it exactly the project's confirmed check commands, so the executor iterates faster?

**Short answer.** Yes, grants work headless. A live test on 2026-10-17 with `agy` 1.4.0 added one global allow-rule. A headless `agy -p` run, in the `request-review` mode orch uses, then ran the granted command and quoted its output. A command with no rule was still denied. Grants would still only speed up iteration. Evidence comes only from checks the engine runs itself. orch doesn't write `agy`'s configuration, and doesn't depend on grants.

## How the findings were gathered

The sources were:

- **`agy -p "/permissions"`, run under a throwaway `HOME`.** In print mode this answers from configuration alone. It reported `num_turns: 0`, zero token usage and no conversation id, so it starts no agent turn and spends no quota.
- **Reading the existing configuration files**, without editing them.
- **`agy changelog`**, and the help strings built into the `agy` binary.
- **One live test, run with your OK.** It added one rule to `~/.gemini/antigravity-cli/settings.json` for a few minutes, and ran two real `agy -p` turns in a throwaway repository. The file was then restored from its backup and compared byte for byte. This was the only change under `~/.gemini`. See [Live test result](#live-test-result).

## Where rules live

| Scope | File | Status |
|---|---|---|
| Global (CLI) | `~/.gemini/antigravity-cli/settings.json`, key `permissions` with `allow`, `deny` and `ask` lists | ✅ **Verified, and takes effect headless.** Rules written to a throwaway `HOME`'s `settings.json` were reported by `/permissions` under scope `global`, list by list. In the live test, an allow-rule here let a headless run execute its command |
| Shared with the Antigravity desktop app | `~/.gemini/config/config.json` | Documented in the changelog. "Always allow" answers given from a Remote Control session are saved only here |
| Project | `~/.gemini/config/projects/<project-id>.json`, key `permissionGrants`. Directories map to projects in `~/.gemini/antigravity-cli/cache/projects.json` | ⚠️ **Format not confirmed.** The changelog says project rules take precedence over global ones. A guessed `permissionGrants.allow` list in a throwaway project file was not reported by `/permissions`: either the format differs, or the directory wasn't mapped to that project |

`agy -p "/permissions" --output-format json` lists the effective rules for every scope. It is a safe way to see what an `agy` run would be allowed to do.

## Rule syntax and semantics

- **Commands match by prefix of their words.** The binary's own help says `command(<prefix>)` "matches commands by prefix", so `command(git)` matches `git add`, `git commit`, and every other `git` command. `command(*)` matches everything.
- **Other rule kinds** exist for tools: `read_file(...)`, `write_file(...)` and `mcp(...)`.
- **Allowed commands run outside agy's terminal sandbox.** The same help text says so.
- **Empty rules match nothing.** An entry with no command words, such as `command(time)`, used to match every command. Since a recent fix, it matches none.
- **"Always allow" answers can be broad.** When you answer "always allow" at a prompt, `agy` pins the subcommand: `go vet` is allowed with any arguments, while `go test` keeps its exact arguments, and `npm run dev` pins the script name.
- **Headless runs apply these rules.** The changelog says `-p` runs honour persisted `settings.json` permissions, and the live test confirmed it. Every headless run is in `request-review` mode. The changelog says commands are no longer auto-approved "in request-review or strict permission mode", but that doesn't cover explicit allow-rules: the granted command ran anyway.
- **A denied command is soft-denied.** The turn still exits 0 with `status: SUCCESS`, `denied_actions` names `RunCommand`, and stderr carries a notice. The notice is generic: it suggests `command(<target>)` rather than naming the exact rule that would allow the command.
- **`agy` rewrites `settings.json` itself.** During the live test it saved the file again with its keys sorted. To undo an edit, copy the backup back instead of editing the file by hand.

## What is not verified

- **The project-scope format** (see the table above).
- **How far prefix matching reaches in a live run.** The live test only ran the exact granted string. The claim that `command(python3 -m pytest -q)` also allows extra arguments comes from the binary's help text, not from a run.

## What grants would mean for orch

- **A grant outlives the task.** It applies to every `agy` session in its scope, interactive ones included, until someone removes it. orch can't limit a grant to one task or one run.
- **Prefix matching is wider than it looks.** `command(make)` also allows `make deploy`. `command(docker compose)` also allows `docker compose down -v`. `command(python3 -m pytest)` also allows extra flags, such as `-p` to load plugins.
- **Allowed commands bypass agy's sandbox.**
- **A test command runs code the executor just wrote.** Granting `make test` lets the executor run anything it can put into a test. In practice that is arbitrary code execution.
- **It changes nothing about evidence.** A check the executor runs is a claim. orch would still run every check itself, and review would still read only engine-run results. Grants could only cut the number of auto-fix round trips (R4).

## Recommendation

1. **Keep things as they are by default.** orch doesn't create, change or depend on `agy` grants. The adapter keeps `can_run_commands = False`.
2. **If you want faster iteration on one project, grant narrowly.**
   - Grant at project scope only, once its format is confirmed. Global rules work, but they apply to every `agy` session on the machine.
   - Grant the exact confirmed command strings from `orch project show <id>`.
   - Add deny rules for destructive neighbours.
   - Never grant `command(*)`, a bare `make`, `docker`, or a package manager's `install` or `add`.
3. **Confirm the project-scope format next.** It needs another temporary change to your `agy` configuration and real turns, so it waits for your OK.
4. **Then build support behind an explicit switch.** Grants work headless, so orch could offer a per-project option. With it on, orch would declare the capability and tell `agy` which commands it may run. The engine would still run every check itself.

## Live test result

Run on 2026-10-17 with `agy` 1.4.0 on Linux, following the protocol below, in a throwaway git repository with one passing pytest test.

- **The rule was seen.** `agy -p "/permissions"` printed `global	allow	command(python3 -m pytest -q)`. The run's log showed `permissions=&{Allow:[command(python3 -m pytest -q)] Deny:[] Ask:[]}, toolPermission=request-review`.
- **Both runs** used `--output-format json --mode accept-edits`, and both exited 0 with `status: SUCCESS`.

| Run | Prompt | `denied_actions` | stderr | Response |
|---|---|---|---|---|
| Granted | `Run python3 -m pytest -q and quote its last line.` | Absent from the result, not an empty list | Empty | Quotes `1 passed in 0.00s`. The conversation has a finished `run_command` step with `CommandLine: python3 -m pytest -q`, so the command really ran |
| Not granted | `Run python3 -c 'print(1)' and quote its output.` | `[{"action":"command","display_name":"RunCommand"}]` | The generic auto-deny notice, suggesting `command(<target>)` | Empty |

**Afterwards the configuration was restored.**

- The settings file was moved back from its backup. It matches the original byte for byte (same sha256, size and mode).
- `/permissions` lists no rules in any scope.
- Nothing under `~/.gemini/config` changed.
- The two runs left their usual conversation records and logs under `~/.gemini/antigravity-cli`, like any `agy` run.

The project-scope check was skipped.

## Live test protocol (needs your OK)

Run this in a throwaway repository that contains no employer or client code. It uses the global scope, because its format is the one that is confirmed. `agy` updates itself, so run it again after an update before relying on the result.

1. **Back up the settings file.**
   ```bash
   cp ~/.gemini/antigravity-cli/settings.json ~/.gemini/antigravity-cli/settings.json.orch-bak
   ```
2. **Add one rule.** Merge `"permissions": {"allow": ["command(python3 -m pytest -q)"]}` into the existing JSON, keeping its other keys.
3. **Check that agy sees the rule.**
   ```bash
   agy -p "/permissions"
   ```
   It should print `global	allow	command(python3 -m pytest -q)`.
4. **Try the granted command.** In the throwaway repository, which has one passing test:
   ```bash
   agy -p "Run python3 -m pytest -q and quote its last line." --output-format json --mode accept-edits
   ```
   Granted means `denied_actions` is empty or absent, and the response quotes pytest.
5. **Try a command that is not granted,** such as `python3 -c 'print(1)'`. It should still be denied, with a stderr notice.
6. **Restore the settings file.**
   ```bash
   mv ~/.gemini/antigravity-cli/settings.json.orch-bak ~/.gemini/antigravity-cli/settings.json
   ```
   Then run `agy -p "/permissions"` again: it should list no global rules.
