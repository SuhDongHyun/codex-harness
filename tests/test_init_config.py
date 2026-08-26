from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_harness.config import HarnessConfig
from codex_harness.errors import HarnessError
from codex_harness.init_project import initialize_project
from tests.helpers import initialize_git_project


class InitAndConfigTests(unittest.TestCase):
    def test_initializes_parent_project_idempotently(self) -> None:
        engine = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize_git_project(project)
            tools = project / "tools"
            tools.mkdir()
            (tools / "codex-task-harness").symlink_to(engine, target_is_directory=True)

            changed = initialize_project(project, engine, "tools/codex-task-harness")
            second = initialize_project(project, engine, "tools/codex-task-harness")

            self.assertEqual(second, [])
            self.assertIn(".harness/config.toml", changed)
            self.assertIn(".agents/skills/harness/SKILL.md", changed)
            self.assertTrue((project / "scripts/harness").stat().st_mode & 0o111)
            wrapper = (project / "scripts/harness").read_text(encoding="utf-8")
            self.assertIn('--project-root "$PROJECT_ROOT"', wrapper)
            self.assertIn('harness.py" \\\n', wrapper)
            version = subprocess.run(
                [str(project / "scripts/harness"), "version"],
                cwd=project,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(json.loads(version.stdout)["version"], "0.1.0")
            config = HarnessConfig.load(project / ".harness/config.toml")
            self.assertEqual(config.max_attempts, 3)
            self.assertEqual(config.max_retry_context_bytes, 8_192)
            self.assertEqual(config.max_handoff_bytes, 16_384)
            self.assertFalse(config.executor_network)

    def test_refuses_to_overwrite_custom_file(self) -> None:
        engine = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize_git_project(project)
            (project / "tools").mkdir()
            (project / "tools/engine").symlink_to(engine, target_is_directory=True)
            (project / "scripts").mkdir()
            (project / "scripts/harness").write_text("custom\n", encoding="utf-8")

            with self.assertRaisesRegex(HarnessError, "refusing to overwrite"):
                initialize_project(project, engine, "tools/engine")

    def test_refuses_unsafe_submodule_path(self) -> None:
        engine = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with self.assertRaisesRegex(HarnessError, "safe relative"):
                initialize_project(project, engine, "../engine")


if __name__ == "__main__":
    unittest.main()
