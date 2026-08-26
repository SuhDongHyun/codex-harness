# Codex Task Harness

A lightweight, submodule-ready harness that turns one coding goal into ordered
tasks and executes every task and retry in a fresh Codex session.

The controller, rather than the model, owns state and completion. It checks Git
scope after every agent attempt and independently runs each task's verification
commands before writing a bounded handoff for the next task.

## Core workflow

```text
goal -> plan -> approve -> task-01 (fresh session) -> verify -> handoff
                        -> task-02 (fresh session) -> verify -> handoff
                        -> final verification -> complete
```

The engine is designed to live inside another Git repository as a read-only
submodule. All project-specific configuration and run state live in the parent
repository, never in this engine checkout.

## Add it to a project

```bash
git submodule add https://github.com/SuhDongHyun/codex-harness.git tools/codex-task-harness
python3 tools/codex-task-harness/scripts/harness.py \
  --project-root "$PWD" init \
  --submodule-path tools/codex-task-harness
```

`init` creates the following parent-project integration files without
overwriting unrelated content:

```text
.harness/config.toml
.agents/skills/harness/SKILL.md
scripts/harness
.gitignore                 # adds .harness/runs/
```

Then use the generated wrapper:

```bash
./scripts/harness plan "Implement the login flow"
./scripts/harness status <run-id>
./scripts/harness approve <run-id>
./scripts/harness run <run-id>
./scripts/harness resume <run-id>
./scripts/harness retry-task <run-id> <task-id|final>
```

`plan` is read-only. Review `.harness/runs/<run-id>/plan.json` before approval.
`run` starts only from an approved state. `resume` recovers a controller process
that stopped while running. `retry-task` explicitly reopens a failed or blocked
task after the underlying problem has been addressed.

## Runtime requirements

- Linux or WSL
- Python 3.11+
- Git
- an installed and authenticated Codex CLI

No runtime Python packages are required. The harness reuses normal Codex CLI
authentication. Nested Codex runs ignore user config but do not require a
separate `CODEX_HOME` or login.

## State layout

```text
.harness/runs/<run-id>/
├── request.md
├── plan.json
├── state.json
├── events.jsonl
├── handoffs/
└── evidence/
```

Run state is intentionally ignored by Git. The reviewed plan can be exported or
committed separately if a project wants durable plan review in version control.

## Deliberate non-features

- no parallel writers
- no live dashboard
- no autonomous review model
- no Git commits, branches, pushes, resets, or stashes
- no dedicated Codex runtime home
- no accumulated conversation context between tasks

See [DESIGN.md](DESIGN.md) for authority, recovery, and handoff contracts.

## Development checks

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q codex_harness tests scripts/harness.py
ruff check codex_harness tests scripts/harness.py
ruff format --check codex_harness tests scripts/harness.py
uvx --from mypy mypy --strict codex_harness tests
```
