from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from codex_harness.errors import HarnessError, ValidationError
from codex_harness.git_guard import GitGuard, paths_outside_allowed
from codex_harness.store import RunStore
from tests.helpers import initialize_git_project


class GitAndStoreTests(unittest.TestCase):
    def test_git_guard_detects_scope_and_index_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git_project(root)
            guard = GitGuard(root)
            before = guard.snapshot()
            (root / "app.txt").write_text("after\n", encoding="utf-8")
            after = guard.snapshot()

            self.assertIsNone(guard.safety_error(before, after, ("app.txt",)))
            self.assertIn("outside", guard.safety_error(before, after, ("src/",)) or "")
            self.assertEqual(paths_outside_allowed({"src/a.py"}, ("src/",)), set())

    def test_store_restores_metadata_and_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            store.create("run-test", "goal")
            snapshot = store.capture("run-test")
            store.write_text("run-test", "evidence/new.txt", "new")
            store.restore("run-test", snapshot)
            self.assertFalse(store.path("run-test", "evidence/new.txt").exists())
            store.path("run-test", "bad-link").symlink_to("request.md")
            with self.assertRaisesRegex(ValidationError, "symlink"):
                store.capture("run-test")

    def test_live_lock_is_rejected_and_stale_lock_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            store.create("run-test", "goal")
            lock = store.path("run-test", ".lock")
            lock.write_text(f'{{"pid":{os.getpid()}}}', encoding="utf-8")
            with (
                self.assertRaisesRegex(HarnessError, "live process"),
                store.lock("run-test"),
            ):
                pass
            lock.write_text('{"pid":999999999}', encoding="utf-8")
            with store.lock("run-test"):
                self.assertTrue(lock.exists())
            self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
