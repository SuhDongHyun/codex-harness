from __future__ import annotations

from pathlib import Path

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
max_retry_context_bytes = 8192
max_handoff_bytes = 16384
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


def initialize_project(
    project_root: Path,
    engine_root: Path,
    *,
    force: bool = False,
) -> list[str]:
    project = project_root.resolve()
    engine = engine_root.resolve()
    if project == engine:
        raise HarnessError(
            "init must target a parent project, not the engine repository"
        )
    files = {
        project / ".harness" / "config.toml": CONFIG,
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
    if _merge_gitignore(project / ".gitignore"):
        changed.append(".gitignore")
    return sorted(changed)


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
