from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from .errors import ValidationError
from .models import Task, TaskReport
from .verifier import VerificationResult


def bounded_text(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    marker = f"\n... truncated from {len(raw)} bytes ...\n".encode()
    if len(marker) >= limit:
        return raw[:limit].decode("utf-8", errors="ignore")
    content_limit = max(0, limit - len(marker))
    head_size = content_limit // 2
    tail_size = content_limit - head_size
    head = raw[:head_size].decode("utf-8", errors="ignore")
    tail = raw[-tail_size:].decode("utf-8", errors="ignore") if tail_size else ""
    return head + marker.decode() + tail


def build_handoff(
    task: Task,
    task_state: Mapping[str, object],
    report: TaskReport,
    verification: VerificationResult,
    max_bytes: int,
) -> dict[str, object]:
    changed_files = _changed_files(task_state)
    handoff: dict[str, object] = {
        "version": 1,
        "task": task.id,
        "summary": bounded_text(report.summary, 1_000),
        "changed_files": [bounded_text(path, 512) for path in changed_files[:100]],
        "public_contracts": [
            bounded_text(item, 500) for item in report.public_contracts[:10]
        ],
        "decisions": [bounded_text(item, 500) for item in report.decisions[:10]],
        "verification": [
            {
                "argv": [bounded_text(argument, 512) for argument in command.argv[:20]],
                "exit_code": command.exit_code,
            }
            for command in verification.commands[:30]
        ],
        "remaining_risks": [
            bounded_text(item, 500) for item in report.remaining_risks[:10]
        ],
        "truncated": False,
    }
    if _source_was_truncated(task_state, report, verification):
        handoff["truncated"] = True
    _fit_handoff(handoff, max_bytes)
    return handoff


def serialized_size(value: Mapping[str, object]) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _fit_handoff(handoff: dict[str, object], max_bytes: int) -> None:
    removable = (
        "decisions",
        "remaining_risks",
        "verification",
        "public_contracts",
        "changed_files",
    )
    while serialized_size(handoff) > max_bytes:
        largest = _largest_list(handoff, removable)
        if largest is None:
            summary = handoff["summary"]
            if not isinstance(summary, str):
                raise ValidationError("handoff.summary must be a string")
            overflow = serialized_size(handoff) - max_bytes
            new_limit = max(0, len(summary.encode("utf-8")) - overflow - 64)
            handoff["summary"] = bounded_text(summary, new_limit)
            if serialized_size(handoff) > max_bytes:
                handoff["summary"] = ""
            if serialized_size(handoff) > max_bytes:
                raise ValidationError("handoff byte limit is too small")
            break
        items = handoff[largest]
        if not isinstance(items, list):
            raise ValidationError(f"handoff.{largest} must be an array")
        items.pop()
        handoff["truncated"] = True


def _largest_list(handoff: Mapping[str, object], names: Sequence[str]) -> str | None:
    populated = []
    for name in names:
        value = handoff[name]
        if isinstance(value, list) and value:
            populated.append((serialized_size({name: value}), name))
    return max(populated, default=(0, None))[1]


def _changed_files(task_state: Mapping[str, object]) -> list[str]:
    value = task_state.get("changed_files")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError("task state changed_files must be a string array")
    return value


def _source_was_truncated(
    task_state: Mapping[str, object],
    report: TaskReport,
    verification: VerificationResult,
) -> bool:
    changed_files = _changed_files(task_state)
    return (
        len(report.summary.encode("utf-8")) > 1_000
        or len(changed_files) > 100
        or any(len(path.encode("utf-8")) > 512 for path in changed_files[:100])
        or len(report.public_contracts) > 10
        or any(len(item.encode("utf-8")) > 500 for item in report.public_contracts[:10])
        or len(report.decisions) > 10
        or any(len(item.encode("utf-8")) > 500 for item in report.decisions[:10])
        or len(verification.commands) > 30
        or any(
            len(command.argv) > 20
            or any(
                len(argument.encode("utf-8")) > 512 for argument in command.argv[:20]
            )
            for command in verification.commands[:30]
        )
        or len(report.remaining_risks) > 10
        or any(len(item.encode("utf-8")) > 500 for item in report.remaining_risks[:10])
    )
