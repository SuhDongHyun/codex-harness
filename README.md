# Codex Task Harness

A Codex skill backed by a lightweight controller that turns one coding goal
into ordered tasks and executes every task and retry in a fresh Codex session.

The controller, rather than the model, owns state and completion. It checks Git
scope after every agent attempt and independently runs each task's verification
commands before writing a bounded handoff for the next task.

Verification is workspace-confined and has loopback-only network access for
local service tests. Reported unittest or pytest skips fail verification rather
than silently weakening completion evidence. For Python write scopes, available
Ruff and mypy/pyright executables are mandatory task and final plan gates.

## Core workflow

```text
goal -> plan (fresh retry if rejected) -> approve -> task-01 (fresh session) -> verify -> handoff
                        -> task-02 (fresh session) -> verify -> handoff
                        -> final verification -> complete
```

The repository is installed directly as the parent project's `harness` skill.
Its controller engine remains read-only while project-specific configuration
and run state live in the parent repository.

## Install the skill

```bash
git submodule add \
  https://github.com/SuhDongHyun/codex-harness.git \
  .agents/skills/harness
```

Start a new Codex session in the parent project so it discovers the skill, then
initialize project-specific state:

```console
$harness init
```

`init` creates `.harness/config.toml` and adds `.harness/runs/` to the parent
project's `.gitignore`. It does not copy the skill or create a terminal wrapper.

## Use it from Codex

```console
$harness plan Implement the login flow
$harness status <run-id>
$harness approve <run-id>
$harness run <run-id>
$harness resume <run-id>
$harness retry-task <run-id> <task-id|final>
```

`plan` is read-only. Schema-invalid or approval-ineligible planner output is
retried in a new ephemeral session with bounded rejection evidence. Review
`.harness/runs/<run-id>/plan.json` before approval.
The plan records the instruction and documentation files that informed it with
controller-calculated SHA-256 hashes, and tasks name the applicable sources in
their `read_files`. Approval rejects changed or writable context sources. `run`
starts only from an approved state and rechecks those hashes. `resume` recovers
a controller process that stopped while running. `retry-task` explicitly reopens
a failed or blocked task after the underlying problem has been addressed; a
separate `run` request starts the reopened work.

## Runtime requirements

- Linux or WSL
- Python 3.11+
- Git
- an installed and authenticated Codex CLI

No runtime Python packages or terminal command installation are required. The
skill invokes its bundled `engine` package and reuses normal Codex CLI
authentication. Nested Codex runs ignore user config but do not require a
separate `CODEX_HOME` or login.

## Clone a parent project

Clone with the skill submodule included:

```bash
git clone --recurse-submodules <parent-repository-url>
```

For an existing clone:

```bash
git submodule update --init --recursive
```

Start a new Codex session after the submodule is available.

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
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q engine scripts/harness.py
ruff check engine tests scripts/harness.py
ruff format --check engine tests scripts/harness.py
uvx --from mypy mypy --strict engine tests
```
