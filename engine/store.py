from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path

from .errors import HarnessError, ValidationError

RUN_ID = re.compile(r"run-[a-z0-9-]+$")


class RunStore:
    def __init__(self, runs_root: Path):
        self.runs_root = runs_root.resolve()

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise ValidationError(f"unsafe run id: {run_id!r}")
        return self.runs_root / run_id

    def create(self, run_id: str, goal: str) -> None:
        directory = self.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "evidence").mkdir()
        (directory / "handoffs").mkdir()
        self.write_text(run_id, "request.md", goal.rstrip() + "\n")

    def path(self, run_id: str, name: str) -> Path:
        if not name or Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValidationError(f"unsafe artifact name: {name!r}")
        return self.run_dir(run_id) / name

    def read_json(self, run_id: str, name: str) -> dict[str, object]:
        path = self.path(run_id, name)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValidationError(f"cannot read {path}: {error}") from error
        if not isinstance(value, dict):
            raise ValidationError(f"{path} must contain an object")
        return value

    def write_json(self, run_id: str, name: str, value: Mapping[str, object]) -> None:
        self.write_text(
            run_id,
            name,
            json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        )

    def write_text(self, run_id: str, name: str, value: str) -> None:
        _atomic_write(self.path(run_id, name), value.encode("utf-8"))

    def append_event(self, run_id: str, event: Mapping[str, object]) -> None:
        payload = dict(event)
        payload.setdefault("timestamp", now_iso())
        path = self.path(run_id, "events.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    def capture(self, run_id: str) -> dict[str, bytes]:
        root = self.run_dir(run_id)
        captured: dict[str, bytes] = {}
        for current, directories, files in os.walk(
            root, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            for directory in list(directories):
                path = current_path / directory
                if path.is_symlink():
                    raise ValidationError(
                        f"controller run directory contains symlink: {path}"
                    )
            for filename in files:
                path = current_path / filename
                if path.is_symlink():
                    raise ValidationError(
                        f"controller run directory contains symlink: {path}"
                    )
                captured[path.relative_to(root).as_posix()] = path.read_bytes()
        return captured

    def restore(self, run_id: str, snapshot: Mapping[str, bytes]) -> None:
        root = self.run_dir(run_id)
        current = self.capture(run_id)
        for relative in set(current) - set(snapshot):
            path = root / relative
            if path.is_file():
                path.unlink()
        for relative, value in snapshot.items():
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise ValidationError(f"unsafe snapshot path: {relative!r}")
            destination = root / path
            _ensure_real_parents(root, destination.parent)
            _atomic_write(destination, value)

    def lock(self, run_id: str) -> RunLock:
        return RunLock(self.path(run_id, ".lock"))


class RunLock(AbstractContextManager[None]):
    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def __enter__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                if self._owner_is_alive():
                    raise HarnessError(
                        f"run is already owned by a live process: {self.path}"
                    ) from None
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "created_at": now_iso()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            self.acquired = True
            return None
        raise HarnessError(f"could not acquire run lock: {self.path}")

    def __exit__(self, *args: object) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)

    def _owner_is_alive(self) -> bool:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            pid = value.get("pid")
            if not isinstance(pid, int) or pid <= 0:
                return False
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except (OSError, ValueError, json.JSONDecodeError):
            return False


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _ensure_real_parents(root: Path, directory: Path) -> None:
    relative = directory.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or current.exists() and not current.is_dir():
            raise ValidationError(f"unsafe controller artifact parent: {current}")
        current.mkdir(exist_ok=True)
