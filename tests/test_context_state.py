from __future__ import annotations

import unittest

from engine.context import bounded_text, build_handoff, serialized_size
from engine.errors import ValidationError
from engine.models import Plan, TaskReport
from engine.state import new_state, parse_state
from engine.verifier import CommandEvidence, VerificationResult
from tests.helpers import completed_report, plan_payload


class ContextAndStateTests(unittest.TestCase):
    def test_bounded_text_respects_utf8_byte_limit_and_keeps_both_ends(self) -> None:
        value = "시작-" + "가" * 2_000 + "-끝"

        bounded = bounded_text(value, 1_024)

        self.assertLessEqual(len(bounded.encode("utf-8")), 1_024)
        self.assertTrue(bounded.startswith("시작-"))
        self.assertTrue(bounded.endswith("-끝"))
        self.assertIn("truncated from", bounded)

    def test_handoff_respects_total_serialized_byte_limit(self) -> None:
        plan = Plan.from_dict(plan_payload())
        report_payload = completed_report("요약" * 2_000)
        report_payload["decisions"] = ["결정" * 1_000 for _ in range(20)]
        report_payload["public_contracts"] = ["계약" * 1_000 for _ in range(20)]
        report_payload["remaining_risks"] = ["위험" * 1_000 for _ in range(20)]
        report = TaskReport.from_dict(report_payload)
        command = CommandEvidence(
            argv=("python3", *("인자" * 300 for _ in range(30))),
            exit_code=0,
            stdout="",
            stderr="",
            duration_seconds=0.01,
            timed_out=False,
        )
        verification = VerificationResult(True, (command,) * 30)
        task_state: dict[str, object] = {
            "changed_files": [f"경로-{index}-" + "가" * 300 for index in range(100)]
        }

        handoff = build_handoff(
            plan.tasks[0], task_state, report, verification, max_bytes=2_048
        )

        self.assertLessEqual(serialized_size(handoff), 2_048)
        self.assertIs(handoff["truncated"], True)

    def test_parse_state_rejects_invalid_nested_task_state(self) -> None:
        plan = Plan.from_dict(plan_payload())
        state = new_state("run-test", plan)
        state["tasks"][0]["attempts"] = -1

        with self.assertRaisesRegex(ValidationError, "non-negative integer"):
            parse_state(state, "run-test")


if __name__ == "__main__":
    unittest.main()
