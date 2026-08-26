from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CommandEvidence:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": round(self.duration_seconds, 6),
            "timed_out": self.timed_out,
        }


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    commands: tuple[CommandEvidence, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "commands": [command.to_dict() for command in self.commands],
        }

    def failure_summary(self) -> str:
        if self.ok or not self.commands:
            return ""
        command = self.commands[-1]
        detail = command.stderr.strip() or command.stdout.strip() or "no output"
        return (
            f"command failed ({command.exit_code}): {' '.join(command.argv)}\n{detail}"
        )


class VerificationRunner(Protocol):
    def verify(
        self, commands: Sequence[Sequence[str]], cwd: Path
    ) -> VerificationResult: ...


class Verifier:
    def __init__(
        self,
        codex_command: str,
        timeout_seconds: int,
        max_output_bytes: int,
        *,
        sandboxed: bool = True,
    ):
        self.codex_command = codex_command
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.sandboxed = sandboxed

    def verify(
        self, commands: Sequence[Sequence[str]], cwd: Path
    ) -> VerificationResult:
        evidence: list[CommandEvidence] = []
        for raw in commands:
            argv = tuple(raw)
            started = time.monotonic()
            timed_out = False
            stdout_path = _temporary_path()
            stderr_path = _temporary_path()
            try:
                with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                    process = subprocess.Popen(
                        self._command(argv, cwd),
                        cwd=cwd,
                        env=self._environment(),
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        shell=False,
                        start_new_session=(os.name != "nt"),
                    )
                    try:
                        process.wait(timeout=self.timeout_seconds)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        _terminate(process)
                        process.wait()
                item = CommandEvidence(
                    argv=argv,
                    exit_code=124 if timed_out else (process.returncode or 0),
                    stdout=_bounded(stdout_path, self.max_output_bytes),
                    stderr=_bounded(stderr_path, self.max_output_bytes),
                    duration_seconds=time.monotonic() - started,
                    timed_out=timed_out,
                )
            except OSError as error:
                item = CommandEvidence(
                    argv=argv,
                    exit_code=127,
                    stdout="",
                    stderr=str(error),
                    duration_seconds=time.monotonic() - started,
                    timed_out=False,
                )
            finally:
                stdout_path.unlink(missing_ok=True)
                stderr_path.unlink(missing_ok=True)
            evidence.append(item)
            if item.exit_code != 0:
                return VerificationResult(False, tuple(evidence))
        return VerificationResult(True, tuple(evidence))

    def _command(self, argv: tuple[str, ...], cwd: Path) -> list[str]:
        if not self.sandboxed:
            return list(argv)
        return [
            self.codex_command,
            "sandbox",
            "--permission-profile",
            ":workspace",
            "--cd",
            str(cwd),
            "-c",
            "sandbox_workspace_write.writable_roots=[]",
            "-c",
            "sandbox_workspace_write.network_access=false",
            "--",
            "/usr/bin/env",
            "-u",
            "CODEX_HOME",
            "-u",
            "OPENAI_API_KEY",
            "-u",
            "CODEX_API_KEY",
            *argv,
        ]

    @staticmethod
    def _environment() -> dict[str, str]:
        environment = os.environ.copy()
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "HARNESS_PROJECT_ROOT"):
            environment.pop(name, None)
        return environment


def _temporary_path() -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".harness-verify-")
    os.close(descriptor)
    return Path(name)


def _bounded(path: Path, limit: int) -> str:
    raw = path.read_bytes()
    if len(raw) <= limit:
        return raw.decode("utf-8", errors="replace")
    half = max(1, limit // 2)
    return (
        raw[:half].decode("utf-8", errors="replace")
        + f"\n... truncated from {len(raw)} bytes ...\n"
        + raw[-half:].decode("utf-8", errors="replace")
    )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
