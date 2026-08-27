# Design

## Domain language

- **Run**: one user goal and its complete execution history.
- **Task**: one ordered, independently verifiable unit of work.
- **Attempt**: one fresh Codex process for a task. Retries are new attempts.
- **Handoff**: bounded, verified context passed from one completed task to the
  immediately following task.
- **Evidence**: agent telemetry, Git scope observations, and controller-run
  verification results.

## Authority

`HarnessController` is the only component allowed to mutate `state.json`.
Structured model output may report `completed`, `failed`, or `blocked`, but it
cannot complete a task. A task completes only when:

1. the Codex process completed with a schema-valid report;
2. Git branch, HEAD, and index did not change;
3. every changed path is inside that task's `write_paths`;
4. the agent did not change controller-owned run metadata; and
5. controller-owned verification passed without changing the repository.

## Context isolation

Every planner, task, and retry call launches `codex exec --ephemeral`. Executor
calls are never resumed or forked. A task prompt contains only:

- the run goal;
- the current task contract;
- the immediately preceding verified handoff, if one exists; and
- the last failure summary for the current task retry.

The repository files remain the source of truth. Handoffs are limited to file
names, public contracts, decisions, verification summaries, and remaining
risks. Earlier handoffs are not accumulated into later prompts.

## Parent-project boundary

The repository root is a Codex skill mounted at
`.agents/skills/harness` in the parent project. The controller engine and its
schemas live under that read-only skill root. The project root is an explicit
controller argument and must be the parent Git repository.

The engine never writes to the skill submodule during normal use. The
`$harness init` command writes only `.harness/config.toml` and the run-state
ignore entry to the parent project. Run artifacts live under that parent's
`.harness/runs` directory.

## Skill interface

`SKILL.md` is the user-facing interface. It routes `$harness init`, `plan`,
`approve`, `run`, `status`, `resume`, and `retry-task` to the deterministic
controller entrypoint. The skill does not duplicate controller state changes or
verification logic. Planning and approval remain separate user requests.

## Recovery

The controller writes task state and the pre-attempt Git snapshot before each
Codex launch. If the controller stops:

- `resume` accepts changes made by the interrupted attempt only when they remain
  inside the current task's `write_paths` and Git branch/HEAD/index are intact;
- a task interrupted during verification is verified again without requiring a
  model call;
- an interrupted executor attempt is continued with a new ephemeral attempt;
- a live run lock prevents a second writer; a lock owned by a dead local process
  is reclaimed automatically.

Failed or blocked states require explicit `retry-task`. This command validates
any additional user edits against the failed task's scope before creating a new
approved checkpoint. A safety violation is stricter: the user must first restore
the exact pre-violation Git snapshot, because the controller cannot safely infer
which changes should be retained.

## Token discipline

- sequential tasks only;
- bounded event and final-result files;
- one previous handoff with a configurable total byte limit, never a growing
  summary list;
- bounded retry failure context while full verification evidence remains on
  disk;
- medium reasoning by default;
- configurable higher reasoning for retries;
- aggregate usage recorded in `state.json`.

The harness records usage but does not claim that session isolation alone saves
tokens. Representative project tasks should be benchmarked before changing
model or reasoning defaults.
