from __future__ import annotations

import json

from .models import Task


def planning_prompt(goal: str) -> str:
    return (
        "Create a complete, minimal sequential implementation plan for the exact "
        "goal below. Minimal means few tasks and a narrow change surface, never "
        "reduced user-visible behavior or weaker proof of completion. Turn "
        "underspecified product intent into concrete, observable acceptance "
        "behavior in task objectives without narrowing the stated goal. "
        "Do not edit files. Return only the JSON object required by the output "
        "schema. Each task will run in a completely fresh Codex session, so every "
        "task must be self-contained and must name the exact repository files it "
        "needs to read. Inspect applicable project instructions and documentation "
        "while planning. List the repository-relative paths of documentation or "
        "instruction files actually used to form the plan in context_sources; use "
        "an empty array when none apply. Include each context source in read_files "
        "for every task it governs, and never include a context source in any "
        "write_paths. Do not list ordinary implementation code as a context source. "
        "The controller will calculate and pin source hashes. Use task IDs task-01, "
        "task-02, and so on in execution order. Keep one independently verifiable "
        "objective per task. Use narrow repository-relative write_paths. Express "
        "verification as argv arrays without shell operators. Before choosing "
        "verification, inspect project instructions, manifests, CI, and available "
        "tooling. Include every applicable quality gate: focused and full tests, "
        "lint and formatting checks, static type checks, build or compile checks, "
        "and runtime or interaction smoke checks for user-visible behavior. Do not "
        "treat one category, such as syntax compilation, as evidence for another. "
        "For new code in an otherwise unconfigured repository, use standard tools "
        "already available in the execution environment; never invent a command "
        "that requires an unavailable tool or outbound installation. Set network "
        "to false unless that specific task genuinely needs outbound access. The "
        "final_verify commands must prove the whole goal across all applicable "
        "quality categories. Do not create planning-only, "
        "review-only, commit, or push tasks. The goal field must equal this exact "
        "JSON string: "
        f"{json.dumps(goal, ensure_ascii=False)}.\n\nGoal:\n{goal}"
    )


def task_prompt(
    *,
    goal: str,
    task: Task,
    previous_handoff: dict[str, object] | None,
    last_error: str | None,
    network_enabled: bool,
) -> str:
    handoff = previous_handoff or {"status": "none"}
    error = last_error or "None"
    network = "ENABLED" if network_enabled else "DISABLED"
    return (
        "Execute exactly one task in a fresh session. Make the smallest correct "
        "change and return only the JSON object required by the output schema. "
        "Do not edit .harness/runs, Git metadata, the Git index, branches, or "
        "commits. The controller will inspect scope and independently run every "
        "verification command. Report blocked only when progress requires user "
        "action or an external state change; normal implementation or test "
        "failures are failed.\n\n"
        f"Run goal:\n{goal}\n\n"
        f"Task ID: {task.id}\n"
        f"Task name: {task.name}\n"
        f"Objective: {task.objective}\n"
        f"Read first: {json.dumps(task.read_files, ensure_ascii=False)}\n"
        f"Write scope: {json.dumps(task.write_paths, ensure_ascii=False)}\n"
        f"Controller verification: {json.dumps(task.verify, ensure_ascii=False)}\n"
        f"Effective outbound network: {network}\n\n"
        "Immediately preceding verified handoff:\n"
        f"{json.dumps(handoff, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"Previous attempt failure evidence:\n{error}\n"
    )


def agent_failure(
    *,
    exit_code: int,
    stderr: str,
    timed_out: bool,
    terminal_event: str | None,
    malformed_events: int,
    result_truncated: bool,
) -> str:
    if timed_out:
        return "Codex execution timed out"
    if result_truncated:
        return "Codex structured result exceeded the configured size limit"
    if malformed_events:
        return f"Codex emitted {malformed_events} malformed JSONL events"
    if stderr.strip():
        return stderr.strip()[-4_000:]
    return f"Codex execution failed: exit={exit_code}, terminal={terminal_event!r}"
