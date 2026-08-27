# Codex Task Harness

This repository contains a small, reusable Codex execution engine intended to
be mounted read-only at `.agents/skills/harness` in another project. The root
`SKILL.md` is the user-facing interface; `engine/` is its implementation.

## Invariants

- Support Python 3.11 or newer with the standard library only at runtime.
- Expose parent-project usage through `$harness` skill requests, not a required
  terminal installation or generated wrapper.
- Treat the directory passed through `--project-root` as the target repository.
- Never write runtime state inside the engine repository or submodule.
- Only `HarnessController` may change run or task state.
- Every planner, task, and retry invocation is a fresh `codex exec --ephemeral`
  process. Never resume an executor conversation.
- Model output is a report. Controller-owned verification and Git evidence
  decide completion.
- Preserve pre-existing user changes. Never stage, commit, push, revert, or
  delete them automatically.
- Execute tasks sequentially. Parallel writers are out of scope.
- Keep handoffs bounded and pass only the immediately preceding verified
  handoff to the next task.
- Do not add a dashboard, autonomous reviewer, dedicated `CODEX_HOME`, tmux,
  notifications, or automatic Git operations to the core.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q engine scripts/harness.py
ruff check engine tests scripts/harness.py
ruff format --check engine tests scripts/harness.py
uvx --from mypy mypy --strict engine tests
```
