from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_harness.config import HarnessConfig
from codex_harness.controller import HarnessController
from codex_harness.errors import HarnessError
from codex_harness.git_guard import GitGuard
from codex_harness.store import RunStore

from .helpers import (
    FakeRunner,
    FakeVerifier,
    blocked_report,
    completed_report,
    failed_report,
    initialize_git_project,
    plan_payload,
)


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        initialize_git_project(self.root)
        self.store = RunStore(self.root / ".harness/runs")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def controller(
        self,
        runner: FakeRunner,
        verifier: FakeVerifier | None = None,
        *,
        max_attempts: int = 3,
        engine_root: Path | None = None,
    ) -> HarnessController:
        return HarnessController(
            project_root=self.root,
            config=HarnessConfig(
                max_attempts=max_attempts,
                sandbox_verification=False,
            ),
            store=self.store,
            runner=runner,
            verifier=verifier or FakeVerifier(),
            git_guard=GitGuard(self.root),
            engine_root=engine_root,
        )

    def plan_and_approve(
        self, controller: HarnessController, goal: str = "change app"
    ) -> str:
        run_id = controller.plan(goal)
        controller.approve(run_id)
        return run_id

    def test_fresh_task_completes_with_bounded_handoff_and_usage(self) -> None:
        def change_app(_request: object) -> None:
            (self.root / "app.txt").write_text("after\n", encoding="utf-8")

        runner = FakeRunner(
            [plan_payload(), completed_report()], callbacks=[None, change_app]
        )
        verifier = FakeVerifier([True, True])
        controller = self.controller(runner, verifier)
        run_id = self.plan_and_approve(controller)

        state = controller.run(run_id)

        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(runner.requests), 2)
        self.assertEqual(runner.requests[0].sandbox, "read-only")
        self.assertEqual(runner.requests[1].sandbox, "workspace-write")
        self.assertEqual(state["usage"]["input_tokens"], 20)  # type: ignore[index]
        handoff = self.store.read_json(run_id, "handoffs/task-01.json")
        self.assertEqual(handoff["changed_files"], ["app.txt"])
        self.assertEqual(len(verifier.calls), 2)

    def test_failed_attempt_uses_high_effort_fresh_retry(self) -> None:
        def change_app(_request: object) -> None:
            (self.root / "app.txt").write_text("after\n", encoding="utf-8")

        runner = FakeRunner(
            [plan_payload(), failed_report(), completed_report()],
            callbacks=[None, None, change_app],
        )
        controller = self.controller(runner)
        run_id = self.plan_and_approve(controller)

        state = controller.run(run_id)

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["tasks"][0]["attempts"], 2)  # type: ignore[index]
        self.assertEqual(runner.requests[1].reasoning_effort, "medium")
        self.assertEqual(runner.requests[2].reasoning_effort, "high")
        self.assertIn("Previous attempt failure evidence", runner.requests[2].prompt)

    def test_out_of_scope_change_fails_until_user_restores_it(self) -> None:
        def unsafe_change(_request: object) -> None:
            (self.root / "other.txt").write_text("unsafe\n", encoding="utf-8")

        runner = FakeRunner([plan_payload(), completed_report()], [None, unsafe_change])
        controller = self.controller(runner)
        run_id = self.plan_and_approve(controller)

        state = controller.run(run_id)

        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["safety_violation"])
        with self.assertRaisesRegex(HarnessError, "pre-violation snapshot"):
            controller.retry_task(run_id, "task-01")
        (self.root / "other.txt").unlink()
        reopened = controller.retry_task(run_id, "task-01")
        self.assertEqual(reopened["status"], "approved")
        self.assertTrue(reopened["tasks"][0]["retry_profile"])  # type: ignore[index]

    def test_blocked_task_can_be_explicitly_reopened(self) -> None:
        runner = FakeRunner([plan_payload(), blocked_report()])
        controller = self.controller(runner)
        run_id = self.plan_and_approve(controller)

        state = controller.run(run_id)
        reopened = controller.retry_task(run_id, "task-01")
        runner.payloads.append(completed_report())
        completed = controller.run(run_id)

        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["required_action"], "create the fixture")
        self.assertEqual(reopened["tasks"][0]["attempts"], 0)  # type: ignore[index]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(runner.requests[-1].reasoning_effort, "high")

    def test_verification_mutation_is_a_safety_failure(self) -> None:
        def change_app(_request: object) -> None:
            (self.root / "app.txt").write_text("after\n", encoding="utf-8")

        def mutate_during_verification(_root: Path) -> None:
            (self.root / "verification-leak.txt").write_text(
                "unsafe\n", encoding="utf-8"
            )

        runner = FakeRunner(
            [plan_payload(), completed_report()], callbacks=[None, change_app]
        )
        verifier = FakeVerifier([True], [mutate_during_verification])
        controller = self.controller(runner, verifier)
        run_id = self.plan_and_approve(controller)

        state = controller.run(run_id)

        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["safety_violation"])
        with self.assertRaisesRegex(HarnessError, "pre-violation snapshot"):
            controller.retry_task(run_id, "task-01")

    def test_final_verification_can_be_retried_without_model_call(self) -> None:
        def change_app(_request: object) -> None:
            (self.root / "app.txt").write_text("after\n", encoding="utf-8")

        runner = FakeRunner(
            [plan_payload(), completed_report()], callbacks=[None, change_app]
        )
        verifier = FakeVerifier([True, False, True])
        controller = self.controller(runner, verifier)
        run_id = self.plan_and_approve(controller)
        failed = controller.run(run_id)

        reopened = controller.retry_task(run_id, "final")
        completed = controller.run(run_id)

        self.assertEqual(failed["failure_stage"], "final")
        self.assertEqual(reopened["status"], "approved")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(runner.requests), 2)

    def test_resume_restarts_interrupted_task_in_new_attempt(self) -> None:
        runner = FakeRunner([plan_payload(), completed_report()])
        controller = self.controller(runner)
        run_id = self.plan_and_approve(controller)
        state = controller.status(run_id)
        task_state = state["tasks"][0]  # type: ignore[index]
        task_state.update(
            status="running",
            attempts=1,
            baseline_snapshot=state["workspace_snapshot"],
            attempt_snapshot=state["workspace_snapshot"],
        )
        state.update(status="running", current_task="task-01")
        self.store.write_json(run_id, "state.json", state)

        resumed = controller.resume(run_id)

        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["tasks"][0]["attempts"], 2)  # type: ignore[index]
        self.assertEqual(runner.requests[-1].reasoning_effort, "high")

    def test_korean_goal_produces_safe_run_id(self) -> None:
        goal = "로그인 기능을 구현해줘"
        runner = FakeRunner([plan_payload(goal)])
        controller = self.controller(runner)

        run_id = controller.plan(goal)

        self.assertRegex(run_id, r"^run-[a-z0-9-]+$")
        self.assertTrue(self.store.run_dir(run_id).exists())

    def test_approval_rejects_write_scope_overlapping_engine(self) -> None:
        payload = plan_payload()
        payload["tasks"][0]["write_paths"] = ["tools/**"]  # type: ignore[index]
        runner = FakeRunner([payload])
        engine = self.root / "tools/codex-task-harness"
        controller = self.controller(runner, engine_root=engine)
        run_id = controller.plan("change app")

        with self.assertRaisesRegex(HarnessError, "read-only harness engine"):
            controller.approve(run_id)


if __name__ == "__main__":
    unittest.main()
