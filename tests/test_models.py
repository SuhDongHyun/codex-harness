from __future__ import annotations

import unittest

from codex_harness.errors import ValidationError
from codex_harness.models import Plan, TaskReport

from .helpers import completed_report, plan_payload


class PlanTests(unittest.TestCase):
    def test_accepts_strict_sequential_plan(self) -> None:
        plan = Plan.from_dict(plan_payload())

        self.assertEqual(plan.tasks[0].id, "task-01")
        self.assertEqual(plan.tasks[0].write_paths, ("app.txt",))
        self.assertEqual(plan.sha256(), Plan.from_dict(plan.to_dict()).sha256())

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
