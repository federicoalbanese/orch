# Security

## Reporting a vulnerability

Please report vulnerabilities privately, through GitHub's **Report a vulnerability** (Security → Advisories) on this repository, rather than in a public issue.

## Security model in brief

- **Actions reserved for a person:**
  - confirming commands,
  - opting a project in to an external engine,
  - approving tasks,
  - accepting results that need a human.

  The actor comes from `ORCH_ACTOR`, which `--actor` cannot override.
- **Never automatic:** force-push, production deployment, merging into protected branches, discarding uncommitted changes, destructive migrations, and secret or production-infrastructure changes. These can't be configured away.
- **Only person-confirmed commands run.** Commands found in model output or repository text are never executed.
- **Untrusted data.** Executor output, diffs and logs are treated as data. Secrets are redacted before evidence is stored or shown.
- **Code may leave the machine.** Every task sent to an external engine (such as Antigravity) sends that repository's code to its provider, which is why projects are opt-in.
