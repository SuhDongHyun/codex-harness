from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import GitError, ValidationError


@dataclass(frozen=True)
class GitSnapshot:
    branch: str
    head: str
    porcelain: str
    dirty_paths: tuple[str, ...]
    dirty_hashes: dict[str, str]
    index_fingerprint: str
    fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "head": self.head,
            "porcelain": self.porcelain,
            "dirty_paths": list(self.dirty_paths),
            "dirty_hashes": self.dirty_hashes,
            "index_fingerprint": self.index_fingerprint,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, value: object) -> GitSnapshot:
        if not isinstance(value, Mapping):
            raise ValidationError("Git snapshot must be an object")
        try:
            dirty_paths = value["dirty_paths"]
            dirty_hashes = value["dirty_hashes"]
            if not isinstance(dirty_paths, list) or not all(
                isinstance(path, str) for path in dirty_paths
            ):
                raise TypeError
            if not isinstance(dirty_hashes, dict) or not all(
                isinstance(key, str) and isinstance(item, str)
                for key, item in dirty_hashes.items()
            ):
                raise TypeError
            strings = {
                name: value[name]
                for name in (
                    "branch",
                    "head",
                    "porcelain",
                    "index_fingerprint",
                    "fingerprint",
                )
            }
            if not all(isinstance(item, str) for item in strings.values()):
                raise TypeError
        except (KeyError, TypeError) as error:
            raise ValidationError("invalid Git snapshot") from error
        return cls(
            branch=strings["branch"],
            head=strings["head"],
            porcelain=strings["porcelain"],
            dirty_paths=tuple(dirty_paths),
            dirty_hashes=dict(dirty_hashes),
            index_fingerprint=strings["index_fingerprint"],
            fingerprint=strings["fingerprint"],
        )


class GitGuard:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def assert_repository(self) -> None:
        actual = Path(self._git("rev-parse", "--show-toplevel").strip()).resolve()
        if actual != self.root:
            raise GitError(
                f"project root must be the Git top level: expected {actual}, "
                f"received {self.root}"
            )

    def snapshot(self) -> GitSnapshot:
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
        head = self._git("rev-parse", "HEAD").strip()
        porcelain = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        dirty_paths = tuple(sorted(_porcelain_paths(porcelain)))
        dirty_hashes = {path: self._hash_path(path) for path in dirty_paths}
        index_fingerprint = hashlib.sha256(
            self._git("ls-files", "--stage", "-z").encode(
                "utf-8", errors="surrogateescape"
            )
        ).hexdigest()
        canonical = json.dumps(
            {
                "branch": branch,
                "head": head,
                "porcelain": porcelain,
                "dirty_hashes": dirty_hashes,
                "index_fingerprint": index_fingerprint,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return GitSnapshot(
            branch=branch,
            head=head,
            porcelain=porcelain,
            dirty_paths=dirty_paths,
            dirty_hashes=dirty_hashes,
            index_fingerprint=index_fingerprint,
            fingerprint=hashlib.sha256(canonical).hexdigest(),
        )

    @staticmethod
    def changed_paths(before: GitSnapshot, after: GitSnapshot) -> set[str]:
        paths = set(before.dirty_paths) | set(after.dirty_paths)
        return {
            path
            for path in paths
            if before.dirty_hashes.get(path) != after.dirty_hashes.get(path)
        } | (set(after.dirty_paths) - set(before.dirty_paths))

    @staticmethod
    def safety_error(
        before: GitSnapshot,
        after: GitSnapshot,
        allowed_paths: Iterable[str],
    ) -> str | None:
        if before.branch != after.branch or before.head != after.head:
            return "Git branch or HEAD changed"
        if before.index_fingerprint != after.index_fingerprint:
            return "Git index changed"
        outside = paths_outside_allowed(
            GitGuard.changed_paths(before, after), allowed_paths
        )
        if outside:
            return "files changed outside task write_paths: " + ", ".join(
                sorted(outside)
            )
        return None

    def _hash_path(self, relative: str) -> str:
        path = self.root / relative
        if path.is_symlink():
            return (
                "symlink:"
                + hashlib.sha256(path.readlink().as_posix().encode("utf-8")).hexdigest()
            )
        if path.is_dir() and (path / ".git").exists():
            result = subprocess.run(
                ["git", "status", "--porcelain=v1", "-z"],
                cwd=path,
                capture_output=True,
                check=False,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=path,
                capture_output=True,
                check=False,
            )
            return (
                "submodule:"
                + hashlib.sha256(result.stdout + b"\0" + head.stdout).hexdigest()
            )
        if not path.is_file():
            return "missing"
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "Git command failed")
        return result.stdout


def paths_outside_allowed(paths: Iterable[str], patterns: Iterable[str]) -> set[str]:
    allowed = tuple(patterns)
    return {
        path
        for path in paths
        if not any(_matches(path, pattern) for pattern in allowed)
    }


def _matches(path: str, pattern: str) -> bool:
    clean = pattern.rstrip("/")
    return (
        path == clean
        or path.startswith(clean + "/")
        or fnmatch.fnmatchcase(path, pattern)
    )


def _porcelain_paths(porcelain: str) -> set[str]:
    records = porcelain.split("\0")
    paths: set[str] = set()
    skip_next = False
    for record in records:
        if not record:
            continue
        if skip_next:
            paths.add(record)
            skip_next = False
            continue
        if len(record) < 4:
            continue
        status = record[:2]
        paths.add(record[3:])
        if "R" in status or "C" in status:
            skip_next = True
    return paths
