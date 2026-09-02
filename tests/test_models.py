from __future__ import annotations

import json
import unittest
from pathlib import Path

from engine.errors import ValidationError
from engine.models import Plan, TaskReport
from tests.helpers import completed_report, plan_payload


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key for child in value.values() for key in _nested_keys(child)
        }
    if isinstance(value, list):
        return {key for child in value for key in _nested_keys(child)}
    return set()


class PlanTests(unittest.TestCase):
    def test_output_schemas_use_only_codex_supported_constraints(self) -> None:
        schemas = Path(__file__).parents[1] / "engine" / "schemas"
        forbidden = {"$schema", "minLength", "uniqueItems"}

        for path in schemas.glob("*.schema.json"):
            with self.subTest(schema=path.name):
                document = json.loads(path.read_text(encoding="utf-8"))
                keys = _nested_keys(document)
                self.assertTrue(forbidden.isdisjoint(keys), forbidden & keys)

    def test_accepts_strict_sequential_plan(self) -> None:
        plan = Plan.from_dict(plan_payload())

        self.assertEqual(plan.tasks[0].id, "task-01")
        self.assertEqual(plan.tasks[0].write_paths, ("app.txt",))
        self.assertEqual(plan.sha256(), Plan.from_dict(plan.to_dict()).sha256())

    def test_context_sources_must_be_read_by_a_task(self) -> None:
        payload = plan_payload()
        payload["context_sources"] = [{"path": "docs/PRD.md", "sha256": "a" * 64}]

        with self.assertRaisesRegex(ValidationError, "task read_files"):
            Plan.from_dict(payload)

        payload["tasks"][0]["read_files"].append("docs/PRD.md")  # type: ignore[index]
        plan = Plan.from_dict(payload)
        self.assertEqual(plan.context_sources[0].path, "docs/PRD.md")

    def test_rejects_protected_write_path(self) -> None:
        payload = plan_payload()
        payload["tasks"][0]["write_paths"] = [".harness/runs/run-x"]  # type: ignore[index]

        with self.assertRaisesRegex(ValidationError, "protected path"):
            Plan.from_dict(payload)

    def test_rejects_shell_and_mutating_git_verification(self) -> None:
        for command in (["bash", "-c", "true"], ["git", "commit", "-m", "x"]):
            with self.subTest(command=command):
                payload = plan_payload()
                payload["tasks"][0]["verify"] = [command]  # type: ignore[index]
                with self.assertRaises(ValidationError):
                    Plan.from_dict(payload)

    def test_task_report_requires_outcome_evidence(self) -> None:
        report = TaskReport.from_dict(completed_report())
        self.assertEqual(report.outcome, "completed")

        payload = completed_report("")
        with self.assertRaisesRegex(ValidationError, "requires summary"):
            TaskReport.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
