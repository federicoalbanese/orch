# Contributing

Thanks for helping! orch is small on purpose. Please keep it that way.

## Setup

```bash
git clone https://github.com/federicoalbanese/orch.git && cd orch
```

```bash
python3 -m pytest
```

There are no runtime dependencies; only `pytest` is needed for the tests. The tests never touch your real configuration, your repositories, or the network.

## Principles

- **Standard library only.** A new runtime dependency needs a very good reason.
- **Safety lives in the engine, not in prompts.** If a rule matters, enforce it in code, and add a test that fails when the rule is broken.
- **Evidence over claims.** Never trust an executor's report where the engine can check for itself.
- **Verify integrations; don't assume them.** Executor behaviour goes in the adapter, backed by recorded fixtures (`tests/fixtures/`).
- **Small, reviewable changes.** Each change gets tests, and user-visible behaviour gets docs.

## Adding an executor

1. Implement `orch.executors.base.ExecutionEngine`. Declare your `Capabilities` honestly, especially `sends_code_off_machine` and `cumulative_usage`.
2. Register it in `orch/executors/__init__.py`.
3. Add contract tests that replay real recorded output, the way `tests/test_agy_adapter.py` does.

## Pull requests

- Run `python3 -m pytest` before opening one.
- Describe what changed and how you verified it.
