from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from .errors import HarnessError
from .store import _atomic_write

CONFIG = """[harness]
codex_command = "codex"
max_attempts = 3
agent_timeout_seconds = 1800
verification_timeout_seconds = 900
max_event_bytes = 1000000
max_result_bytes = 100000
max_verification_bytes = 100000
executor_network = false
sandbox_verification = true

[planner]
model = "gpt-5.6-terra"
reasoning_effort = "medium"

[executor]
model = "gpt-5.6-terra"
reasoning_effort = "medium"

[retry]
model = "gpt-5.6-terra"
reasoning_effort = "high"
"""

SKILL = """---
name: harness
description: >-
  Plan, approve, run, resume, and inspect sequential fresh-session Codex harness
  runs for this repository.
---

# Harness

Use `./scripts/harness` as the only workflow entry point.

## New goal

1. Run `./scripts/harness plan "<exact user goal>"`.
2. Read the printed run ID and `.harness/runs/<run-id>/plan.json`.
3. Report the ordered tasks, write scopes, and verification commands.
4. Do not approve until the user explicitly approves that exact run ID.

## Approved execution

1. Run `./scripts/harness approve <run-id>` only after explicit user approval.
2. Run `./scripts/harness run <run-id>`.
3. Report controller state and evidence. Do not substitute model completion text
   for controller-owned verification.

## Recovery

- Use `./scripts/harness resume <run-id>` only for a run interrupted while
  running or verifying.
- Use `./scripts/harness retry-task <run-id> <task-id|final>` only after the user
  has resolved or accepted the reported failure/blocker.
- Never edit `.harness/runs` manually.
"""


def initialize_project(
    project_root: Path,
    engine_root: Path,
    submodule_path: str,
    *,
    force: bool = False,
) -> list[str]:
    project = project_root.resolve()
    engine = engine_root.resolve()
    if project == engine:
        raise HarnessError(
            "init must target a parent project, not the engine repository"
        )
    relative = _safe_submodule_path(submodule_path)
    expected_engine = (project / relative).resolve()
    if expected_engine != engine:
        raise HarnessError(
            "--submodule-path does not resolve to this engine: "
            f"{relative} -> {expected_engine}, engine is {engine}"
        )

    files = {
        project / ".harness" / "config.toml": CONFIG,
        project / ".agents" / "skills" / "harness" / "SKILL.md": SKILL,
        project / "scripts" / "harness": _wrapper(relative),
    }
    changed: list[str] = []
    for path, content in files.items():
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing == content:
                continue
            if not force:
                raise HarnessError(f"refusing to overwrite existing file: {path}")
        _atomic_write(path, content.encode("utf-8"))
        changed.append(path.relative_to(project).as_posix())
    wrapper = project / "scripts" / "harness"
    wrapper.chmod(wrapper.stat().st_mode | 0o111)
    if _merge_gitignore(project / ".gitignore"):
        changed.append(".gitignore")
    return sorted(changed)


def _wrapper(submodule_path: str) -> str:
    return (
        "#!/usr/bin/env sh\n"
        "set -eu\n"
        'PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
        f'exec python3 "$PROJECT_ROOT/{submodule_path}/scripts/harness.py" \\\n'
        '  --project-root "$PROJECT_ROOT" "$@"\n'
    )


def _safe_submodule_path(value: str) -> str:
    if not value or "\\" in value:
        raise HarnessError("--submodule-path must be a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path in {PurePosixPath(".")}:
        raise HarnessError("--submodule-path must be a safe relative path")
    if not all(re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in path.parts):
        raise HarnessError("--submodule-path contains unsupported characters")
    return path.as_posix()


def _merge_gitignore(path: Path) -> bool:
    line = ".harness/runs/"
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    if any(item.strip() == line for item in content.splitlines()):
        return False
    prefix = content
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix and prefix.strip():
        prefix += "\n"
    prefix += "# Codex Task Harness runtime\n" + line + "\n"
    _atomic_write(path, prefix.encode("utf-8"))
    return True
