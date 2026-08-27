from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from .errors import ValidationError

TASK_ID = re.compile(r"task-(\d{2})$")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*$")
SHELL_OPERATORS = frozenset({"|", "||", "&&", ";", ">", ">>", "<", "<<"})
PROTECTED_PREFIXES = (".git", ".harness/runs")


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    objective: str
    read_files: tuple[str, ...]
    write_paths: tuple[str, ...]
    verify: tuple[tuple[str, ...], ...]
    network: bool = False

    @classmethod
    def from_dict(cls, value: object) -> Task:
        raw = _mapping(value, "task")
        required = {
            "id",
            "name",
            "objective",
            "read_files",
            "write_paths",
            "verify",
            "network",
        }
        _exact_keys(raw, required, "task")
        task_id = _string(raw["id"], "task.id")
        if not TASK_ID.fullmatch(task_id):
            raise ValidationError("task.id must match task-NN")
        name = _string(raw["name"], "task.name")
        if not SLUG.fullmatch(name):
            raise ValidationError("task.name must be a lowercase kebab-case slug")
        network = raw["network"]
        if not isinstance(network, bool):
            raise ValidationError("task.network must be boolean")
        write_paths = _paths(raw["write_paths"], "task.write_paths", patterns=True)
        if not write_paths:
            raise ValidationError("task.write_paths must not be empty")
        verify = _commands(raw["verify"], "task.verify")
        if not verify:
            raise ValidationError("task.verify must not be empty")
        return cls(
            id=task_id,
            name=name,
            objective=_string(raw["objective"], "task.objective"),
            read_files=_paths(raw["read_files"], "task.read_files"),
            write_paths=write_paths,
            verify=verify,
            network=network,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "objective": self.objective,
            "read_files": list(self.read_files),
            "write_paths": list(self.write_paths),
            "verify": [list(command) for command in self.verify],
            "network": self.network,
        }


@dataclass(frozen=True)
class Plan:
    version: int
    goal: str
    tasks: tuple[Task, ...]
    final_verify: tuple[tuple[str, ...], ...]

    @classmethod
    def from_dict(cls, value: object) -> Plan:
        raw = _mapping(value, "plan")
        _exact_keys(raw, {"version", "goal", "tasks", "final_verify"}, "plan")
        if raw["version"] != 1:
            raise ValidationError("plan.version must be 1")
        tasks_raw = raw["tasks"]
        if not isinstance(tasks_raw, list) or not 1 <= len(tasks_raw) <= 50:
            raise ValidationError("plan.tasks must contain between 1 and 50 tasks")
        tasks = tuple(Task.from_dict(item) for item in tasks_raw)
        for index, task in enumerate(tasks, start=1):
            if task.id != f"task-{index:02d}":
                raise ValidationError("task IDs must be sequential from task-01")
        final_verify = _commands(raw["final_verify"], "plan.final_verify")
        if not final_verify:
            raise ValidationError("plan.final_verify must not be empty")
        return cls(
            version=1,
            goal=_string(raw["goal"], "plan.goal"),
            tasks=tasks,
            final_verify=final_verify,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "goal": self.goal,
            "tasks": [task.to_dict() for task in self.tasks],
            "final_verify": [list(command) for command in self.final_verify],
        }

    def sha256(self) -> str:
        canonical = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class TaskReport:
    outcome: str
    summary: str
    error: str | None
    blocked_reason: str | None
    required_action: str | None
    decisions: tuple[str, ...]
    public_contracts: tuple[str, ...]
    remaining_risks: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object) -> TaskReport:
        raw = _mapping(value, "task report")
        _exact_keys(
            raw,
            {
                "outcome",
                "summary",
                "error",
                "blocked_reason",
                "required_action",
                "decisions",
                "public_contracts",
                "remaining_risks",
            },
            "task report",
        )
        outcome = _string(raw["outcome"], "task report.outcome")
        if outcome not in {"completed", "failed", "blocked"}:
            raise ValidationError("task report.outcome is invalid")
        report = cls(
            outcome=outcome,
            summary=_string(raw["summary"], "task report.summary", allow_empty=True),
            error=_optional_string(raw["error"], "task report.error"),
            blocked_reason=_optional_string(
                raw["blocked_reason"], "task report.blocked_reason"
            ),
            required_action=_optional_string(
                raw["required_action"], "task report.required_action"
            ),
            decisions=_strings(raw["decisions"], "task report.decisions"),
            public_contracts=_strings(
                raw["public_contracts"], "task report.public_contracts"
            ),
            remaining_risks=_strings(
                raw["remaining_risks"], "task report.remaining_risks"
            ),
        )
        if outcome == "completed" and not report.summary:
            raise ValidationError("completed task report requires summary")
        if outcome == "failed" and not report.error:
            raise ValidationError("failed task report requires error")
        if outcome == "blocked" and (
            not report.blocked_reason or not report.required_action
        ):
            raise ValidationError(
                "blocked task report requires blocked_reason and required_action"
            )
        return report

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "summary": self.summary,
            "error": self.error,
            "blocked_reason": self.blocked_reason,
            "required_action": self.required_action,
            "decisions": list(self.decisions),
            "public_contracts": list(self.public_contracts),
            "remaining_risks": list(self.remaining_risks),
        }


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be an object")
    return value


def _exact_keys(raw: Mapping[str, object], keys: set[str], label: str) -> None:
    if set(raw) != keys:
        missing = keys - set(raw)
        extra = set(raw) - keys
        detail = []
        if missing:
            detail.append("missing: " + ", ".join(sorted(missing)))
        if extra:
            detail.append("extra: " + ", ".join(sorted(extra)))
        raise ValidationError(f"{label} fields are invalid ({'; '.join(detail)})")


def _string(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string")
    clean = value.strip()
    if not clean and not allow_empty:
        raise ValidationError(f"{label} must not be empty")
    if len(clean) > 8_000:
        raise ValidationError(f"{label} is too long")
    return clean


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 20:
        raise ValidationError(f"{label} must be an array with at most 20 items")
    return tuple(_string(item, f"{label}[]") for item in value)


def _paths(value: object, label: str, *, patterns: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValidationError(f"{label} must be an array with at most 100 items")
    paths: list[str] = []
    for item in value:
        path = _string(item, f"{label}[]")
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or path in {".", ""}:
            raise ValidationError(f"unsafe relative path in {label}: {path!r}")
        if "\\" in path:
            raise ValidationError(f"paths must use forward slashes: {path!r}")
        static = re.split(r"[*?[{]", path, maxsplit=1)[0].rstrip("/")
        if any(
            static == prefix or static.startswith(prefix + "/")
            for prefix in PROTECTED_PREFIXES
        ):
            raise ValidationError(f"protected path in {label}: {path!r}")
        if not patterns and any(character in path for character in "*?[{"):
            raise ValidationError(f"glob not allowed in {label}: {path!r}")
        paths.append(path)
    return tuple(paths)


def _commands(value: object, label: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list) or len(value) > 30:
        raise ValidationError(f"{label} must be an array with at most 30 commands")
    commands: list[tuple[str, ...]] = []
    for raw in value:
        if not isinstance(raw, list) or not raw or len(raw) > 100:
            raise ValidationError(f"{label} commands must be non-empty argv arrays")
        argv = tuple(_string(item, f"{label}[][]") for item in raw)
        if any(item in SHELL_OPERATORS for item in argv):
            raise ValidationError(f"shell operators are forbidden in {label}")
        if argv[0] in {"sh", "bash", "zsh", "fish", "powershell", "pwsh"}:
            raise ValidationError(f"shell interpreters are forbidden in {label}")
        if (
            argv[0] == "git"
            and len(argv) > 1
            and argv[1]
            in {
                "add",
                "checkout",
                "clean",
                "commit",
                "merge",
                "push",
                "rebase",
                "reset",
                "restore",
                "stash",
                "switch",
            }
        ):
            raise ValidationError(f"mutating Git command is forbidden in {label}")
        commands.append(argv)
    return tuple(commands)


def add_usage(total: dict[str, int], usage: Mapping[str, object]) -> None:
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            total[key] = total.get(key, 0) + value
