---
name: harness
description: >-
  Initialize, plan, approve, run, inspect, resume, and retry controller-owned
  Codex task harness runs in the current repository. Use for $harness commands
  and requests to operate this repository's sequential fresh-session harness.
---

# Harness

Operate the harness only through its controller entrypoint. The parent project
root is the Git repository containing this skill at `.agents/skills/harness`.
Run commands from that project root with this pattern:

```bash
python3 .agents/skills/harness/scripts/harness.py --project-root "$PWD" <command>
```

Do not expose the internal Python command unless diagnosing the integration;
describe user-facing operations as `$harness <command>`.

## Commands

### `init`

Run `init`, report which integration files changed, and stop. It creates the
parent project's `.harness/config.toml` and adds `.harness/runs/` to
`.gitignore`. Do not use `--force` unless the user explicitly asks to replace
an existing harness config.

### `plan <goal>`

Pass the exact remaining text as the goal. Read the returned run ID and
`.harness/runs/<run-id>/plan.json`, then report the ordered task objectives,
write scopes, task verification, and final verification. Stop after reporting
the draft. Never infer approval from a planning request.

### `approve <run-id>`

Treat this exact invocation as approval for that run ID. Run `approve`, report
the approved state, and stop. Do not start execution unless the user separately
requests `run`.

### `run <run-id>`

Run the already approved run to a controller-owned terminal state. Report task
outcomes, verification evidence, and any required action. Model completion text
is not evidence of completion.

### `status <run-id>`

Print and summarize controller-owned state without changing it.

### `resume <run-id>`

Use only for a run interrupted in `running` or `verifying`. Report the resumed
terminal state and evidence.

### `retry-task <run-id> <task-id|final>`

Use only after the user has resolved or explicitly accepted the reported
failure or blocker. This reopens the target to `approved`; do not run it unless
the user separately requests `run`.

## Boundaries

- Never edit `.harness/runs` manually.
- Never stage, commit, push, reset, stash, or switch branches for the harness.
- Do not run task commands in parallel.
- If the controller rejects changed Git state or a safety violation, report the
  exact restoration or user action it requires instead of bypassing it.
