from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine.config import HarnessConfig
from engine.errors import HarnessError
from engine.init_project import initialize_project


class InitAndConfigTests(unittest.TestCase):
    def test_skill_entrypoint_initializes_parent_project(self) -> None:
        engine = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            skill_parent = project / ".agents/skills"
            skill_parent.mkdir(parents=True)
            (skill_parent / "harness").symlink_to(engine, target_is_directory=True)

            result = subprocess.run(
                [
                    sys.executable,
                    str(project / ".agents/skills/harness/scripts/harness.py"),
                    "--project-root",
                    str(project),
                    "init",
                ],
                cwd=project,
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            self.assertEqual(payload["changed"], [".gitignore", ".harness/config.toml"])

    def test_initializes_parent_project_idempotently(self) -> None:
        engine = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            changed = initialize_project(project, engine)
            second = initialize_project(project, engine)

            self.assertEqual(second, [])
            self.assertEqual(changed, [".gitignore", ".harness/config.toml"])
            self.assertIn(".harness/runs/", (project / ".gitignore").read_text())
            config = HarnessConfig.load(project / ".harness/config.toml")
            self.assertEqual(config.max_attempts, 3)
            self.assertEqual(config.max_retry_context_bytes, 8_192)
            self.assertEqual(config.max_handoff_bytes, 16_384)
            self.assertFalse(config.executor_network)

    def test_refuses_to_overwrite_custom_file(self) -> None:
        engine = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            config = project / ".harness/config.toml"
            config.parent.mkdir()
            config.write_text("custom\n", encoding="utf-8")

            with self.assertRaisesRegex(HarnessError, "refusing to overwrite"):
                initialize_project(project, engine)

    def test_refuses_to_initialize_engine_repository(self) -> None:
        engine = Path(__file__).resolve().parents[1]
        with self.assertRaisesRegex(HarnessError, "parent project"):
            initialize_project(engine, engine)


if __name__ == "__main__":
    unittest.main()
