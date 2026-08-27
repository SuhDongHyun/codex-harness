from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AgentRequest:
    prompt: str
    cwd: Path
    sandbox: str
    schema: Path
    event_log: Path
    model: str
    reasoning_effort: str
    timeout_seconds: int
    max_event_bytes: int
    max_result_bytes: int
    network: bool = False


@dataclass(frozen=True)
class AgentResult:
    exit_code: int
    payload: dict[str, object] | None
    terminal_event: str | None
    stderr: str
    timed_out: bool
    malformed_events: int
    event_log_truncated: bool
    result_truncated: bool
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and self.terminal_event == "turn.completed"
            and self.payload is not None
            and self.malformed_events == 0
            and not self.result_truncated
        )


class AgentRunner(Protocol):
    def run(self, request: AgentRequest) -> AgentResult: ...


class CodexRunner:
    def __init__(self, codex_command: str = "codex"):
        self.codex_command = codex_command

    def build_command(self, request: AgentRequest, result_path: Path) -> list[str]:
        if request.sandbox not in {"read-only", "workspace-write"}:
            raise ValueError(f"unsupported sandbox: {request.sandbox}")
        if request.network and request.sandbox != "workspace-write":
            raise ValueError("network can be enabled only for workspace-write")
        return [
            self.codex_command,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--json",
            "--model",
            request.model,
            "--sandbox",
            request.sandbox,
            "--output-schema",
            str(request.schema),
            "-o",
            str(result_path),
            "-c",
            'approval_policy="never"',
            "-c",
            f'model_reasoning_effort="{request.reasoning_effort}"',
            "-c",
            "agents.enabled=false",
            "-c",
            "sandbox_workspace_write.writable_roots=[]",
            "-c",
            "sandbox_workspace_write.network_access="
            + ("true" if request.network else "false"),
            "-c",
            (
                'shell_environment_policy.exclude=["OPENAI_API_KEY",'
                '"CODEX_API_KEY","HARNESS_PROJECT_ROOT"]'
            ),
            request.prompt,
        ]

    def run(self, request: AgentRequest) -> AgentResult:
        request.event_log.parent.mkdir(parents=True, exist_ok=True)
        result_path = _temporary_path(request.event_log.parent, ".result.json")
        event_path = _temporary_path(request.event_log.parent, ".events.jsonl")
        stderr_path = _temporary_path(request.event_log.parent, ".stderr.log")
        timed_out = False
        exit_code = 1
        try:
            with (
                event_path.open("wb") as stdout,
                stderr_path.open("wb") as stderr_handle,
            ):
                process = subprocess.Popen(
                    self.build_command(request, result_path),
                    cwd=request.cwd,
                    env=os.environ.copy(),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr_handle,
                    shell=False,
                    start_new_session=(os.name != "nt"),
                )
                try:
                    process.wait(timeout=request.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _terminate(process)
                    process.wait()
                exit_code = process.returncode or 0
            terminal, malformed, usage = _parse_events(event_path)
            event_truncated = _publish_bounded_file(
                event_path, request.event_log, request.max_event_bytes
            )
            payload, result_truncated = _read_payload(
                result_path, request.max_result_bytes
            )
            stderr_text = _read_bounded_text(stderr_path, request.max_event_bytes)
            return AgentResult(
                exit_code=124 if timed_out else exit_code,
                payload=payload,
                terminal_event=terminal,
                stderr=stderr_text,
                timed_out=timed_out,
                malformed_events=malformed,
                event_log_truncated=event_truncated,
                result_truncated=result_truncated,
                usage=usage,
            )
        except OSError as error:
            request.event_log.write_text("", encoding="utf-8")
            return AgentResult(
                exit_code=127,
                payload=None,
                terminal_event=None,
                stderr=str(error),
                timed_out=False,
                malformed_events=0,
                event_log_truncated=False,
                result_truncated=False,
            )
        finally:
            result_path.unlink(missing_ok=True)
            event_path.unlink(missing_ok=True)
            stderr_path.unlink(missing_ok=True)


def _temporary_path(directory: Path, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=".codex-harness-", suffix=suffix, dir=directory
    )
    os.close(descriptor)
    return Path(name)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return


def _parse_events(path: Path) -> tuple[str | None, int, dict[str, int]]:
    terminal: str | None = None
    malformed = 0
    usage: dict[str, int] = {}
    with path.open("rb") as handle:
        for raw in handle:
            try:
                event = json.loads(raw)
                if not isinstance(event, Mapping):
                    raise TypeError
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                malformed += 1
                continue
            event_type = event.get("type")
            if event_type in {"turn.completed", "turn.failed", "turn.cancelled"}:
                terminal = str(event_type)
            raw_usage = event.get("usage")
            if isinstance(raw_usage, Mapping):
                usage = {
                    str(key): value
                    for key, value in raw_usage.items()
                    if isinstance(value, int) and not isinstance(value, bool)
                }
    return terminal, malformed, usage


def _publish_bounded_file(source: Path, destination: Path, limit: int) -> bool:
    size = source.stat().st_size
    if size <= limit:
        os.replace(source, destination)
        return False
    half = max(1, limit // 2)
    with source.open("rb") as handle:
        head = handle.read(half)
        handle.seek(max(0, size - half))
        tail = handle.read(half)
    marker = (f'\n{{"type":"harness.truncated","original_bytes":{size}}}\n').encode()
    destination.write_bytes(head + marker + tail)
    return True


def _read_payload(path: Path, limit: int) -> tuple[dict[str, object] | None, bool]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        return None, False
    if len(raw) > limit:
        return None, True
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, False
    return (value, False) if isinstance(value, dict) else (None, False)


def _read_bounded_text(path: Path, limit: int) -> str:
    raw = path.read_bytes()
    if len(raw) <= limit:
        return raw.decode("utf-8", errors="replace")
    half = max(1, limit // 2)
    return (
        raw[:half].decode("utf-8", errors="replace")
        + f"\n... truncated from {len(raw)} bytes ...\n"
        + raw[-half:].decode("utf-8", errors="replace")
    )
