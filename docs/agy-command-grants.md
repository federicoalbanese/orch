# Scoped `agy` command grants: findings

Date: 2026-10-17. Tool: `agy` 1.4.0 on Linux. Context: Phase 4, risk R4 ([architecture.md](architecture.md)).

**Question.** Headless `agy -p` denies every shell command (`denied_actions: RunCommand`), so it can't run tests itself. `agy` documents allow-rules for commands. Could orch grant it exactly the project's confirmed check commands, so the executor iterates faster?

**Short answer.** The rules exist and their format is confirmed. Whether they take effect in the headless mode orch uses is not verified yet: that needs a real `agy` turn and a temporary change to your `agy` configuration, which waits for your OK. Even if they work, they would only speed up iteration. Evidence still comes only from checks the engine runs itself. orch doesn't write `agy`'s configuration, and doesn't depend on grants.

## How the findings were gathered

Nothing under `~/.gemini` was changed. The sources were:

- **`agy -p "/permissions"`, run under a throwaway `HOME`.** In print mode this answers from configuration alone. It reported `num_turns: 0`, zero token usage and no conversation id, so it starts no agent turn and spends no quota.
- **Reading the existing configuration files**, without editing them.
- **`agy changelog`**, and the help strings built into the `agy` binary.

## Where rules live

| Scope | File | Status |
|---|---|---|
| Global (CLI) | `~/.gemini/antigravity-cli/settings.json`, key `permissions` with `allow`, `deny` and `ask` lists | ✅ **Verified.** Rules written to a throwaway `HOME`'s `settings.json` were reported by `/permissions` under scope `global`, list by list |
| Shared with the Antigravity desktop app | `~/.gemini/config/config.json` | Documented in the changelog. "Always allow" answers given from a Remote Control session are saved only here |
| Project | `~/.gemini/config/projects/<project-id>.json`, key `permissionGrants`. Directories map to projects in `~/.gemini/antigravity-cli/cache/projects.json` | ⚠️ **Format not confirmed.** The changelog says project rules take precedence over global ones. A guessed `permissionGrants.allow` list in a throwaway project file was not reported by `/permissions`: either the format differs, or the directory wasn't mapped to that project |

`agy -p "/permissions" --output-format json` lists the effective rules for every scope. It is a safe way to see what an `agy` run would be allowed to do.

## Rule syntax and semantics

- **Commands match by prefix of their words.** The binary's own help says `command(<prefix>)` "matches commands by prefix", so `command(git)` matches `git add`, `git commit`, and every other `git` command. `command(*)` matches everything.
- **Other rule kinds** exist for tools: `read_file(...)`, `write_file(...)` and `mcp(...)`.
- **Allowed commands run outside agy's terminal sandbox.** The same help text says so.
- **Empty rules match nothing.** An entry with no command words, such as `command(time)`, used to match every command. Since a recent fix, it matches none.
- **"Always allow" answers can be broad.** When you answer "always allow" at a prompt, `agy` pins the subcommand: `go vet` is allowed with any arguments, while `go test` keeps its exact arguments, and `npm run dev` pins the script name.
- **Headless runs read these rules.** The changelog says `-p` runs honour persisted `settings.json` permissions. A denied tool is soft-denied, with a stderr notice that names the rule that would allow it.

## What is not verified

- **Whether a granted command runs headless.** Phase 1 probing ran without grants, and `RunCommand` was denied in every mode tried. Every headless run reports `permission_mode: request-review` in its `init` event. The changelog also says commands are no longer auto-approved "in request-review or strict permission mode". That may mean grants are ignored in exactly the mode orch uses.
- **The project-scope format** (see the table above).

## What grants would mean for orch

- **A grant outlives the task.** It applies to every `agy` session in its scope, interactive ones included, until someone removes it. orch can't limit a grant to one task or one run.
- **Prefix matching is wider than it looks.** `command(make)` also allows `make deploy`. `command(docker compose)` also allows `docker compose down -v`. `command(python3 -m pytest)` also allows extra flags, such as `-p` to load plugins.
- **Allowed commands bypass agy's sandbox.**
- **A test command runs code the executor just wrote.** Granting `make test` lets the executor run anything it can put into a test. In practice that is arbitrary code execution.
- **It changes nothing about evidence.** A check the executor runs is a claim. orch would still run every check itself, and review would still read only engine-run results. Grants could only cut the number of auto-fix round trips (R4).

## Recommendation

1. **Keep things as they are by default.** orch doesn't create, change or depend on `agy` grants. The adapter keeps `can_run_commands = False`.
2. **If you want faster iteration on one project, grant narrowly.**
   - Grant at project scope only, once its format is confirmed.
   - Grant the exact confirmed command strings from `orch project show <id>`.
   - Add deny rules for destructive neighbours.
   - Never grant `command(*)`, a bare `make`, `docker`, or a package manager's `install` or `add`.
3. **Verify live before orch relies on it.** The protocol below needs your OK, because it changes your `agy` configuration for a few minutes and runs real turns.
4. **Then build support behind an explicit switch.** If grants work headless, orch could offer a per-project option. With it on, orch would declare the capability and tell `agy` which commands it may run. The engine would still run every check itself.

## Live test protocol (needs your OK)

Run this in a throwaway repository that contains no employer or client code. It uses the global scope, because its format is the one that is confirmed.

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
   Granted means `denied_actions` is empty and the response quotes pytest.
5. **Try a command that is not granted,** such as `python3 -c 'print(1)'`. It should still be denied, with a stderr notice.
6. **Restore the settings file.**
   ```bash
   mv ~/.gemini/antigravity-cli/settings.json.orch-bak ~/.gemini/antigravity-cli/settings.json
   ```
   Then run `agy -p "/permissions"` again: it should list no global rules.
