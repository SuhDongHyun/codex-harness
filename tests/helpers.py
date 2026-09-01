from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from engine.runner import AgentRequest, AgentResult
from engine.verifier import CommandEvidence, VerificationResult


def git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def initialize_git_project(root: Path) -> None:
    git("init", "-q", cwd=root)
    git("config", "user.email", "tests@example.com", cwd=root)
    git("config", "user.name", "Harness Tests", cwd=root)
    (root / ".gitignore").write_text(".harness/runs/\n", encoding="utf-8")
    (root / "app.txt").write_text("before\n", encoding="utf-8")
    git("add", ".gitignore", "app.txt", cwd=root)
    git("commit", "-qm", "initial", cwd=root)


def plan_payload(goal: str = "change app") -> dict[str, object]:
    return {
        "goal": goal,
        "context_sources": [],
        "tasks": [
            {
                "id": "task-01",
                "name": "change-app",
                "objective": "Change app.txt",
                "read_files": ["app.txt"],
                "write_paths": ["app.txt"],
                "verify": [["python3", "-c", "print('task ok')"]],
                "network": False,
            }
        ],
        "final_verify": [["python3", "-c", "print('final ok')"]],
    }


def completed_report(summary: str = "changed app") -> dict[str, object]:
    return {
        "outcome": "completed",
        "summary": summary,
        "error": None,
        "blocked_reason": None,
        "required_action": None,
        "decisions": ["kept the change small"],
        "public_contracts": ["app.txt contains the result"],
        "remaining_risks": [],
    }


def failed_report(error: str = "implementation failed") -> dict[str, object]:
    return {
        "outcome": "failed",
        "summary": "",
        "error": error,
        "blocked_reason": None,
        "required_action": None,
        "decisions": [],
        "public_contracts": [],
        "remaining_risks": [],
    }


def blocked_report() -> dict[str, object]:
    return {
        "outcome": "blocked",
        "summary": "",
        "error": None,
        "blocked_reason": "missing fixture",
        "required_action": "create the fixture",
        "decisions": [],
        "public_contracts": [],
        "remaining_risks": [],
    }


class FakeRunner:
    def __init__(
        self,
        payloads: Sequence[dict[str, object]],
        callbacks: Sequence[Callable[[AgentRequest], None] | None] = (),
    ):
        self.payloads = list(payloads)
        self.callbacks = list(callbacks)
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        index = len(self.requests)
        self.requests.append(request)
        callback = self.callbacks[index] if index < len(self.callbacks) else None
        if callback is not None:
            callback(request)
        request.event_log.parent.mkdir(parents=True, exist_ok=True)
        request.event_log.write_text(
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n',
            encoding="utf-8",
        )
        return AgentResult(
            exit_code=0,
            payload=self.payloads[index],
            terminal_event="turn.completed",
            stderr="",
            timed_out=False,
            malformed_events=0,
            event_log_truncated=False,
            result_truncated=False,
            usage={"input_tokens": 10, "output_tokens": 2},
        )


class FakeVerifier:
    def __init__(
        self,
        results: Sequence[bool] = (True, True),
        callbacks: Sequence[Callable[[Path], None] | None] = (),
    ):
        self.results = list(results)
        self.callbacks = list(callbacks)
        self.calls: list[tuple[tuple[str, ...], ...]] = []

    def verify(
        self, commands: Sequence[Sequence[str]], cwd: Path
    ) -> VerificationResult:
        index = len(self.calls)
        normalized = tuple(tuple(command) for command in commands)
        self.calls.append(normalized)
        callback = self.callbacks[index] if index < len(self.callbacks) else None
        if callback is not None:
            callback(cwd)
        ok = self.results[index]
        command = CommandEvidence(
            argv=normalized[0],
            exit_code=0 if ok else 1,
            stdout="ok\n" if ok else "",
            stderr="" if ok else "failed\n",
            duration_seconds=0.01,
            timed_out=False,
        )
        return VerificationResult(ok, (command,))
