from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError

EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})


@dataclass(frozen=True)
class ModelProfile:
    model: str
    reasoning_effort: str

    def validate(self, label: str) -> None:
        if not self.model.strip():
            raise ValidationError(f"{label}.model must not be empty")
        if self.reasoning_effort not in EFFORTS:
            raise ValidationError(
                f"{label}.reasoning_effort must be one of: "
                + ", ".join(sorted(EFFORTS))
            )


@dataclass(frozen=True)
class HarnessConfig:
    codex_command: str = "codex"
    max_attempts: int = 3
    agent_timeout_seconds: int = 1800
    verification_timeout_seconds: int = 900
    max_event_bytes: int = 1_000_000
    max_result_bytes: int = 100_000
    max_verification_bytes: int = 100_000
    executor_network: bool = False
    sandbox_verification: bool = True
    planner: ModelProfile = ModelProfile("gpt-5.6-terra", "medium")
    executor: ModelProfile = ModelProfile("gpt-5.6-terra", "medium")
    retry: ModelProfile = ModelProfile("gpt-5.6-terra", "high")

    @classmethod
    def load(cls, path: Path) -> HarnessConfig:
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ValidationError(f"cannot read config {path}: {error}") from error
        if set(raw) != {"harness", "planner", "executor", "retry"}:
            raise ValidationError(
                "config must contain exactly [harness], [planner], [executor], "
                "and [retry]"
            )
        harness = _table(raw["harness"], "harness")
        allowed = {
            "codex_command",
            "max_attempts",
            "agent_timeout_seconds",
            "verification_timeout_seconds",
            "max_event_bytes",
            "max_result_bytes",
            "max_verification_bytes",
            "executor_network",
            "sandbox_verification",
        }
        extra = set(harness) - allowed
        if extra:
            raise ValidationError("unknown harness fields: " + ", ".join(sorted(extra)))
        config = cls(
            codex_command=_string_option(harness, "codex_command", "codex"),
            max_attempts=_integer_option(harness, "max_attempts", 3),
            agent_timeout_seconds=_integer_option(
                harness, "agent_timeout_seconds", 1800
            ),
            verification_timeout_seconds=_integer_option(
                harness, "verification_timeout_seconds", 900
            ),
            max_event_bytes=_integer_option(harness, "max_event_bytes", 1_000_000),
            max_result_bytes=_integer_option(harness, "max_result_bytes", 100_000),
            max_verification_bytes=_integer_option(
                harness, "max_verification_bytes", 100_000
            ),
            executor_network=_boolean_option(harness, "executor_network", False),
            sandbox_verification=_boolean_option(harness, "sandbox_verification", True),
            planner=_profile(raw["planner"], "planner"),
            executor=_profile(raw["executor"], "executor"),
            retry=_profile(raw["retry"], "retry"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not isinstance(self.codex_command, str) or not self.codex_command.strip():
            raise ValidationError("harness.codex_command must not be empty")
        for name, value, minimum, maximum in (
            ("max_attempts", self.max_attempts, 1, 10),
            ("agent_timeout_seconds", self.agent_timeout_seconds, 1, 7200),
            (
                "verification_timeout_seconds",
                self.verification_timeout_seconds,
                1,
                7200,
            ),
            ("max_event_bytes", self.max_event_bytes, 1024, 10_000_000),
            ("max_result_bytes", self.max_result_bytes, 1024, 1_000_000),
            (
                "max_verification_bytes",
                self.max_verification_bytes,
                1024,
                1_000_000,
            ),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValidationError(f"harness.{name} must be an integer")
            if not minimum <= value <= maximum:
                raise ValidationError(
                    f"harness.{name} must be between {minimum} and {maximum}"
                )
        if not isinstance(self.executor_network, bool):
            raise ValidationError("harness.executor_network must be boolean")
        if not isinstance(self.sandbox_verification, bool):
            raise ValidationError("harness.sandbox_verification must be boolean")
        self.planner.validate("planner")
        self.executor.validate("executor")
        self.retry.validate("retry")

    def codex_path(self) -> str:
        resolved = shutil.which(self.codex_command)
        if resolved is None:
            raise ValidationError(f"Codex command not found: {self.codex_command}")
        return resolved


def _table(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValidationError(f"[{label}] must be a table")
    return value


def _profile(value: object, label: str) -> ModelProfile:
    raw = _table(value, label)
    if set(raw) != {"model", "reasoning_effort"}:
        raise ValidationError(
            f"[{label}] must contain exactly model and reasoning_effort"
        )
    model = raw["model"]
    effort = raw["reasoning_effort"]
    if not isinstance(model, str) or not isinstance(effort, str):
        raise ValidationError(f"[{label}] values must be strings")
    return ModelProfile(model, effort)


def _string_option(raw: dict[str, object], name: str, default: str) -> str:
    value = raw.get(name, default)
    if not isinstance(value, str):
        raise ValidationError(f"harness.{name} must be a string")
    return value


def _integer_option(raw: dict[str, object], name: str, default: int) -> int:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"harness.{name} must be an integer")
    return value


def _boolean_option(raw: dict[str, object], name: str, default: bool) -> bool:
    value = raw.get(name, default)
    if not isinstance(value, bool):
        raise ValidationError(f"harness.{name} must be boolean")
    return value
