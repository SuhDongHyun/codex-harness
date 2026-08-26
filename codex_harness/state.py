from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypedDict, cast

from .errors import ValidationError
from .models import Plan
from .store import now_iso

RunStatus = Literal[
    "draft",
    "approved",
    "running",
    "verifying",
    "completed",
    "failed",
    "blocked",
]
TaskStatus = Literal[
    "pending",
    "running",
    "retrying",
    "verifying",
    "completed",
    "failed",
    "blocked",
]
FailureStage = Literal["task", "final"]


class TaskState(TypedDict):
    id: str
    status: TaskStatus
    attempts: int
    retry_profile: bool
    summary: str | None
    error: str | None
    changed_files: list[str]
    candidate_report: dict[str, object] | None
    baseline_snapshot: dict[str, object] | None
    attempt_snapshot: dict[str, object] | None


class RunState(TypedDict):
    version: int
    run_id: str
    goal: str
    status: RunStatus
    plan_sha256: str | None
    current_task: str | None
    failure_stage: FailureStage | None
    error: str | None
    blocked_reason: str | None
    required_action: str | None
    safety_violation: bool
    workspace_snapshot: dict[str, object] | None
    usage: dict[str, int]
    tasks: list[TaskState]
    created_at: str
    updated_at: str
    completed_at: str | None


RUN_STATUSES = frozenset(
    {"draft", "approved", "running", "verifying", "completed", "failed", "blocked"}
)
TASK_STATUSES = frozenset(
    {"pending", "running", "retrying", "verifying", "completed", "failed", "blocked"}
)
RUN_KEYS = frozenset(RunState.__required_keys__)
TASK_KEYS = frozenset(TaskState.__required_keys__)
USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    }
)


def new_state(run_id: str, plan: Plan) -> RunState:
    now = now_iso()
    tasks: list[TaskState] = [
        {
            "id": task.id,
            "status": "pending",
            "attempts": 0,
            "retry_profile": False,
            "summary": None,
            "error": None,
            "changed_files": [],
            "candidate_report": None,
            "baseline_snapshot": None,
            "attempt_snapshot": None,
        }
        for task in plan.tasks
    ]
    return {
        "version": 1,
        "run_id": run_id,
        "goal": plan.goal,
        "status": "draft",
        "plan_sha256": None,
        "current_task": None,
        "failure_stage": None,
        "error": None,
        "blocked_reason": None,
        "required_action": None,
        "safety_violation": False,
        "workspace_snapshot": None,
        "usage": {key: 0 for key in USAGE_KEYS},
        "tasks": tasks,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }


def parse_state(value: Mapping[str, object], run_id: str) -> RunState:
    _exact_keys(value, RUN_KEYS, "state")
    if value["version"] != 1 or value["run_id"] != run_id:
        raise ValidationError("invalid run state identity")
    _string(value["goal"], "state.goal")
    _choice(value["status"], RUN_STATUSES, "state.status")
    _optional_string(value["plan_sha256"], "state.plan_sha256")
    _optional_string(value["current_task"], "state.current_task")
    failure_stage = value["failure_stage"]
    if failure_stage is not None:
        _choice(failure_stage, frozenset({"task", "final"}), "state.failure_stage")
    _optional_string(value["error"], "state.error")
    _optional_string(value["blocked_reason"], "state.blocked_reason")
    _optional_string(value["required_action"], "state.required_action")
    if not isinstance(value["safety_violation"], bool):
        raise ValidationError("state.safety_violation must be boolean")
    _optional_mapping(value["workspace_snapshot"], "state.workspace_snapshot")
    _validate_usage(value["usage"])
    _validate_tasks(value["tasks"])
    _string(value["created_at"], "state.created_at")
    _string(value["updated_at"], "state.updated_at")
    _optional_string(value["completed_at"], "state.completed_at")
    return cast(RunState, dict(value))


def task_state(state: RunState, task_id: str) -> TaskState:
    for task in state["tasks"]:
        if task["id"] == task_id:
            return task
    raise ValidationError(f"state does not contain {task_id}")


def _validate_usage(value: object) -> None:
    if not isinstance(value, dict):
        raise ValidationError("state.usage must be an object")
    _exact_keys(value, USAGE_KEYS, "state.usage")
    if not all(
        isinstance(item, int) and not isinstance(item, bool) and item >= 0
        for item in value.values()
    ):
        raise ValidationError("state.usage values must be non-negative integers")


def _validate_tasks(value: object) -> None:
    if not isinstance(value, list) or not value:
        raise ValidationError("state.tasks must not be empty")
    seen: set[str] = set()
    for index, item in enumerate(value):
        label = f"state.tasks[{index}]"
        if not isinstance(item, dict):
            raise ValidationError(f"{label} must be an object")
        _exact_keys(item, TASK_KEYS, label)
        task_id = _string(item["id"], f"{label}.id")
        if task_id in seen:
            raise ValidationError(f"duplicate task state: {task_id}")
        seen.add(task_id)
        _choice(item["status"], TASK_STATUSES, f"{label}.status")
        attempts = item["attempts"]
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ValidationError(f"{label}.attempts must be a non-negative integer")
        if not isinstance(item["retry_profile"], bool):
            raise ValidationError(f"{label}.retry_profile must be boolean")
        _optional_string(item["summary"], f"{label}.summary")
        _optional_string(item["error"], f"{label}.error")
        changed = item["changed_files"]
        if not isinstance(changed, list) or not all(
            isinstance(path, str) for path in changed
        ):
            raise ValidationError(f"{label}.changed_files must be a string array")
        _optional_mapping(item["candidate_report"], f"{label}.candidate_report")
        _optional_mapping(item["baseline_snapshot"], f"{label}.baseline_snapshot")
        _optional_mapping(item["attempt_snapshot"], f"{label}.attempt_snapshot")


def _exact_keys(
    value: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    if set(value) != expected:
        raise ValidationError(f"{label} fields are invalid")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ValidationError(f"{label} must be a string or null")


def _choice(value: object, allowed: frozenset[str], label: str) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"invalid {label}")


def _optional_mapping(value: object, label: str) -> None:
    if value is not None and not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object or null")
