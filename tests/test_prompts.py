from __future__ import annotations

import unittest

from engine.prompts import planning_prompt


class PlanningPromptTests(unittest.TestCase):
    def test_requires_complete_scope_and_quality_gates(self) -> None:
        prompt = planning_prompt("build a web service")

        self.assertIn("never reduced user-visible behavior", prompt)
        self.assertIn("observable acceptance behavior", prompt)
        for gate in (
            "full tests",
            "lint and formatting checks",
            "static type checks",
            "build or compile checks",
            "runtime or interaction smoke checks",
        ):
            with self.subTest(gate=gate):
                self.assertIn(gate, prompt)
        self.assertIn("already available in the execution environment", prompt)
        self.assertIn("requires `ruff check`", prompt)
        self.assertIn("mypy or pyright", prompt)


if __name__ == "__main__":
    unittest.main()
