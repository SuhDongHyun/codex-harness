from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from .config import HarnessConfig, ModelProfile
from .errors import HarnessError, ValidationError
from .git_guard import GitGuard, GitSnapshot
from .models import Plan, Task, TaskReport, add_usage
from .prompts import agent_failure, planning_prompt, task_prompt
from .runner import AgentRequest, AgentResult, AgentRunner
from .store import RunStore, now_iso
from .verifier import VerificationResult, VerificationRunner


class HarnessController:
    def __init__(
        self,
        *,
        project_root: Path,
        config: HarnessConfig,
        store: RunStore,
        runner: AgentRunner,
        verifier: VerificationRunner,
        git_guard: GitGuard,
        engine_root: Path | None = None,
    ):
        self.root = project_root.resolve()
        self.config = config
        self.store = store
        self.runner = runner
        self.verifier = verifier
        self.git = git_guard
        self.engine_root = (
            engine_root or Path(__file__).resolve().parent.parent
        ).resolve()
        schema_root = Path(__file__).resolve().parent / "schemas"
        self.plan_schema = schema_root / "plan.schema.json"
        self.task_schema = schema_root / "task-result.schema.json"

    def plan(self, goal: str) -> str:
        clean_goal = goal.strip()
        if not clean_goal:
            raise HarnessError("goal must not be empty")
        self.git.assert_repository()
        run_id = _run_id(clean_goal)
        self.store.create(run_id, clean_goal)
        before_git = self.git.snapshot()
        before_files = self.store.capture(run_id)
        event_log = self.store.path(run_id, "evidence/plan-events.jsonl")
        request = AgentRequest(
            prompt=planning_prompt(clean_goal),
            cwd=self.root,
            sandbox="read-only",
            schema=self.plan_schema,
            event_log=event_log,
            model=self.config.planner.model,
            reasoning_effort=self.config.planner.reasoning_effort,
            timeout_seconds=self.config.agent_timeout_seconds,
            max_event_bytes=self.config.max_event_bytes,
            max_result_bytes=self.config.max_result_bytes,
        )
        result = self.runner.run(request)
        after_git = self.git.snapshot()
        metadata_error = self._metadata_error(
            run_id, before_files, "evidence/plan-events.jsonl"
        )
        self._write_agent_evidence(run_id, "evidence/plan-agent.json", request, result)
        if before_git.fingerprint != after_git.fingerprint:
            raise HarnessError("planner changed the project Git working tree")
        if metadata_error:
            raise HarnessError(metadata_error)
        if not result.succeeded:
            raise HarnessError(_agent_failure(result))
        try:
            plan = Plan.from_dict(result.payload)
        except ValidationError as error:
            raise HarnessError(f"planner returned an invalid plan: {error}") from error
        if plan.goal != clean_goal:
            raise HarnessError("planner changed the requested goal")
        self.store.write_json(run_id, "plan.json", plan.to_dict())
        state = _new_state(run_id, plan)
        add_usage(_usage(state), result.usage)
        self._write_state(state)
        self.store.append_event(
            run_id,
            {
                "type": "plan.created",
                "model": request.model,
                "reasoning_effort": request.reasoning_effort,
                "usage": result.usage,
            },
        )
        return run_id

    def approve(self, run_id: str) -> dict[str, object]:
        with self.store.lock(run_id):
            state = self.status(run_id)
            if state["status"] != "draft":
                raise HarnessError("only a draft run can be approved")
            plan = self._plan(run_id)
            self._assert_engine_protected(plan)
            if (
                any(task.network for task in plan.tasks)
                and not self.config.executor_network
            ):
                requested = ", ".join(task.id for task in plan.tasks if task.network)
                raise HarnessError(
                    f"tasks request network ({requested}) but executor_network is false"
                )
            snapshot = self.git.snapshot()
            state.update(
                status="approved",
                plan_sha256=plan.sha256(),
                workspace_snapshot=snapshot.to_dict(),
                updated_at=now_iso(),
            )
            self._write_state(state)
            self.store.append_event(
                run_id,
                {
                    "type": "plan.approved",
                    "plan_sha256": plan.sha256(),
                    "workspace_fingerprint": snapshot.fingerprint,
                },
            )
            return state

    def _assert_engine_protected(self, plan: Plan) -> None:
        try:
            engine = self.engine_root.relative_to(self.root).as_posix()
        except ValueError:
            return
        for task in plan.tasks:
            for pattern in task.write_paths:
                prefix = re.split(r"[*?[{]", pattern, maxsplit=1)[0]
                static = prefix.rstrip("/")
                has_glob = prefix != pattern
                if (
                    engine == "."
                    or not static
                    or (has_glob and engine.startswith(prefix))
                    or engine == static
                    or engine.startswith(static + "/")
                    or static.startswith(engine + "/")
                ):
                    raise HarnessError(
                        f"{task.id} write_paths overlaps the read-only harness "
                        f"engine ({engine}): {pattern}"
                    )

    def run(self, run_id: str) -> dict[str, object]:
        with self.store.lock(run_id):
            state = self.status(run_id)
            plan = self._plan(run_id)
            if state["status"] != "approved":
                raise HarnessError("run requires approved state")
            self._assert_plan_hash(state, plan)
            expected = GitSnapshot.from_dict(state["workspace_snapshot"])
            current = self.git.snapshot()
            if expected.fingerprint != current.fingerprint:
                raise HarnessError("project changed after approval")
            state.update(status="running", updated_at=now_iso())
            self._write_state(state)
            self.store.append_event(run_id, {"type": "run.started"})
            return self._execute(run_id, state, plan)

    def resume(self, run_id: str) -> dict[str, object]:
        with self.store.lock(run_id):
            state = self.status(run_id)
            plan = self._plan(run_id)
            if state["status"] not in {"running", "verifying"}:
                raise HarnessError("resume requires an interrupted running run")
            self._assert_plan_hash(state, plan)
            current_task_id = state.get("current_task")
            if isinstance(current_task_id, str):
                task = _task_by_id(plan, current_task_id)
                task_state = _task_state(state, current_task_id)
                raw_before = task_state.get("attempt_snapshot") or task_state.get(
                    "baseline_snapshot"
                )
                if raw_before is None:
                    raw_before = state["workspace_snapshot"]
                before = GitSnapshot.from_dict(raw_before)
                error = self.git.safety_error(
                    before, self.git.snapshot(), task.write_paths
                )
                if error:
                    raise HarnessError(f"cannot resume safely: {error}")
                if task_state["status"] == "running":
                    task_state.update(
                        status="retrying",
                        error="controller process was interrupted during execution",
                    )
                    self._write_state(state)
            else:
                expected = GitSnapshot.from_dict(state["workspace_snapshot"])
                if expected.fingerprint != self.git.snapshot().fingerprint:
                    raise HarnessError(
                        "cannot resume: project changed while interrupted"
                    )
            state["status"] = "running"
            state["updated_at"] = now_iso()
            self._write_state(state)
            self.store.append_event(run_id, {"type": "run.resumed"})
            return self._execute(run_id, state, plan)

    def retry_task(self, run_id: str, target: str) -> dict[str, object]:
        with self.store.lock(run_id):
            state = self.status(run_id)
            plan = self._plan(run_id)
            if state["status"] not in {"failed", "blocked"}:
                raise HarnessError("retry-task requires a failed or blocked run")
            self._assert_plan_hash(state, plan)
            current = self.git.snapshot()
            before = GitSnapshot.from_dict(state["workspace_snapshot"])
            if target == "final":
                if state.get("failure_stage") != "final":
                    raise HarnessError("run did not fail during final verification")
                allowed = tuple(
                    path for task in plan.tasks for path in task.write_paths
                )
            else:
                task = _task_by_id(plan, target)
                if state.get("current_task") != target:
                    raise HarnessError(f"{target} is not the failed or blocked task")
                task_state = _task_state(state, target)
                if task_state["status"] not in {"failed", "blocked"}:
                    raise HarnessError(f"{target} is not failed or blocked")
                allowed = task.write_paths
                task_state.update(
                    status="pending",
                    attempts=0,
                    retry_profile=True,
                    error=None,
                    summary=None,
                    candidate_report=None,
                    baseline_snapshot=None,
                    attempt_snapshot=None,
                    changed_files=[],
                )
            if (
                state.get("safety_violation") is True
                and before.fingerprint != current.fingerprint
            ):
                raise HarnessError(
                    "cannot retry safely: restore the workspace to the "
                    "pre-violation snapshot first"
                )
            error = self.git.safety_error(before, current, allowed)
            if error:
                raise HarnessError(f"cannot retry safely: {error}")
            state.update(
                status="approved",
                current_task=None,
                failure_stage=None,
                error=None,
                blocked_reason=None,
                required_action=None,
                safety_violation=False,
                workspace_snapshot=current.to_dict(),
                updated_at=now_iso(),
            )
            self._write_state(state)
            self.store.append_event(
                run_id, {"type": "run.retry_approved", "target": target}
            )
            return state

    def status(self, run_id: str) -> dict[str, object]:
        state = self.store.read_json(run_id, "state.json")
        _validate_state(state, run_id)
        return state

    def _execute(
        self, run_id: str, state: dict[str, object], plan: Plan
    ) -> dict[str, object]:
        for index, task in enumerate(plan.tasks):
            task_state = _task_state(state, task.id)
            if task_state["status"] == "completed":
                continue
            if task_state["status"] == "verifying":
                outcome = self._verify_task(run_id, state, task, task_state)
                if outcome == "completed":
                    continue
                if outcome == "terminal":
                    return state
            terminal = self._run_task(
                run_id,
                state,
                plan,
                task,
                task_state,
                previous_task=plan.tasks[index - 1] if index else None,
            )
            if terminal:
                return state
        return self._finalize(run_id, state, plan)

    def _run_task(
        self,
        run_id: str,
        state: dict[str, object],
        plan: Plan,
        task: Task,
        task_state: dict[str, object],
        *,
        previous_task: Task | None,
    ) -> bool:
        last_error = task_state.get("error")
        if not isinstance(last_error, str):
            last_error = None
        if _attempts(task_state) >= self.config.max_attempts:
            self._fail_task(
                state,
                task_state,
                last_error or "interrupted task exhausted its attempt limit",
                self.git.snapshot(),
            )
            return True
        while _attempts(task_state) < self.config.max_attempts:
            if task_state.get("baseline_snapshot") is None:
                task_state["baseline_snapshot"] = self.git.snapshot().to_dict()
            before = self.git.snapshot()
            attempt = _attempts(task_state) + 1
            task_state.update(
                status="running",
                attempts=attempt,
                error=None,
                attempt_snapshot=before.to_dict(),
            )
            state.update(status="running", current_task=task.id, updated_at=now_iso())
            self._write_state(state)
            profile = (
                self.config.retry
                if attempt > 1 or task_state.get("retry_profile") is True
                else self.config.executor
            )
            previous_handoff = (
                self.store.read_json(run_id, f"handoffs/{previous_task.id}.json")
                if previous_task is not None
                else None
            )
            request = self._task_request(
                run_id,
                plan,
                task,
                attempt,
                profile,
                previous_handoff,
                last_error,
            )
            self.store.append_event(
                run_id,
                {
                    "type": "task.started",
                    "task": task.id,
                    "attempt": attempt,
                    "model": profile.model,
                    "reasoning_effort": profile.reasoning_effort,
                },
            )
            result, metadata_error = self._run_agent_guarded(
                run_id, request, f"evidence/{task.id}-attempt-{attempt:02d}.jsonl"
            )
            after = self.git.snapshot()
            add_usage(_usage(state), result.usage)
            self._write_agent_evidence(
                run_id,
                f"evidence/{task.id}-attempt-{attempt:02d}-agent.json",
                request,
                result,
                changed_files=sorted(self.git.changed_paths(before, after)),
            )
            safety_error = metadata_error or self.git.safety_error(
                before, after, task.write_paths
            )
            if safety_error:
                self._fail_task(
                    state,
                    task_state,
                    safety_error,
                    before,
                    safety_violation=True,
                )
                return True
            if not result.succeeded:
                last_error = _agent_failure(result)
            else:
                try:
                    report = TaskReport.from_dict(result.payload)
                except ValidationError as error:
                    last_error = f"invalid task report: {error}"
                else:
                    if report.outcome == "blocked":
                        self._block_task(state, task_state, report, after)
                        return True
                    if report.outcome == "failed":
                        last_error = report.error or "Codex reported failure"
                    else:
                        task_state.update(
                            status="verifying",
                            candidate_report=report.to_dict(),
                            changed_files=sorted(
                                self.git.changed_paths(
                                    GitSnapshot.from_dict(
                                        task_state["baseline_snapshot"]
                                    ),
                                    after,
                                )
                            ),
                        )
                        self._write_state(state)
                        outcome = self._verify_task(run_id, state, task, task_state)
                        if outcome == "completed":
                            return False
                        if outcome == "terminal":
                            return True
                        last_error = str(
                            task_state.get("error") or "verification failed"
                        )
            if attempt >= self.config.max_attempts:
                self._fail_task(
                    state,
                    task_state,
                    last_error or "task failed without evidence",
                    after,
                )
                return True
            task_state.update(status="retrying", error=last_error)
            state["updated_at"] = now_iso()
            self._write_state(state)
            self.store.append_event(
                run_id,
                {
                    "type": "task.retrying",
                    "task": task.id,
                    "attempt": attempt,
                    "error": last_error,
                },
            )
        return True

    def _verify_task(
        self,
        run_id: str,
        state: dict[str, object],
        task: Task,
        task_state: dict[str, object],
    ) -> str:
        attempt = _attempts(task_state)
        before = self.git.snapshot()
        run_files = self.store.capture(run_id)
        result = self.verifier.verify(task.verify, self.root)
        after = self.git.snapshot()
        metadata_changed = self.store.capture(run_id) != run_files
        if metadata_changed:
            self.store.restore(run_id, run_files)
        self.store.write_json(
            run_id,
            f"evidence/{task.id}-attempt-{attempt:02d}-verification.json",
            result.to_dict(),
        )
        safety_error = None
        if metadata_changed:
            safety_error = "verification changed controller-owned run metadata"
        elif before.fingerprint != after.fingerprint:
            safety_error = "verification changed the project working tree"
        if safety_error:
            self._fail_task(
                state,
                task_state,
                safety_error,
                before,
                safety_violation=True,
            )
            return "terminal"
        if not result.ok:
            task_state.update(status="retrying", error=result.failure_summary())
            self._write_state(state)
            if attempt >= self.config.max_attempts:
                self._fail_task(state, task_state, result.failure_summary(), after)
                return "terminal"
            return "retry"
        report = TaskReport.from_dict(task_state["candidate_report"])
        handoff = _handoff(task, task_state, report, result)
        self.store.write_json(run_id, f"handoffs/{task.id}.json", handoff)
        task_state.update(
            status="completed",
            retry_profile=False,
            summary=report.summary,
            error=None,
            candidate_report=None,
            baseline_snapshot=None,
            attempt_snapshot=None,
        )
        state.update(
            current_task=None,
            workspace_snapshot=after.to_dict(),
            updated_at=now_iso(),
        )
        self._write_state(state)
        self.store.append_event(
            run_id,
            {"type": "task.completed", "task": task.id, "attempt": attempt},
        )
        return "completed"

    def _finalize(
        self, run_id: str, state: dict[str, object], plan: Plan
    ) -> dict[str, object]:
        state.update(
            status="verifying",
            current_task=None,
            failure_stage="final",
            updated_at=now_iso(),
        )
        self._write_state(state)
        before = self.git.snapshot()
        run_files = self.store.capture(run_id)
        result = self.verifier.verify(plan.final_verify, self.root)
        after = self.git.snapshot()
        metadata_changed = self.store.capture(run_id) != run_files
        if metadata_changed:
            self.store.restore(run_id, run_files)
        self.store.write_json(
            run_id, "evidence/final-verification.json", result.to_dict()
        )
        error = None
        if metadata_changed:
            error = "final verification changed controller-owned run metadata"
        elif before.fingerprint != after.fingerprint:
            error = "final verification changed the project working tree"
        elif not result.ok:
            error = result.failure_summary()
        if error:
            safety_violation = (
                metadata_changed or before.fingerprint != after.fingerprint
            )
            state.update(
                status="failed",
                failure_stage="final",
                error=error,
                safety_violation=safety_violation,
                workspace_snapshot=(before if safety_violation else after).to_dict(),
                updated_at=now_iso(),
            )
            self._write_state(state)
            self.store.append_event(
                run_id, {"type": "run.failed", "stage": "final", "error": error}
            )
            return state
        state.update(
            status="completed",
            current_task=None,
            failure_stage=None,
            error=None,
            workspace_snapshot=after.to_dict(),
            updated_at=now_iso(),
            completed_at=now_iso(),
        )
        self._write_state(state)
        self.store.append_event(run_id, {"type": "run.completed"})
        return state

    def _task_request(
        self,
        run_id: str,
        plan: Plan,
        task: Task,
        attempt: int,
        profile: ModelProfile,
        previous_handoff: dict[str, object] | None,
        last_error: str | None,
    ) -> AgentRequest:
        network = self.config.executor_network and task.network
        return AgentRequest(
            prompt=task_prompt(
                goal=plan.goal,
                task=task,
                previous_handoff=previous_handoff,
                last_error=last_error,
                network_enabled=network,
            ),
            cwd=self.root,
            sandbox="workspace-write",
            schema=self.task_schema,
            event_log=self.store.path(
                run_id, f"evidence/{task.id}-attempt-{attempt:02d}.jsonl"
            ),
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            timeout_seconds=self.config.agent_timeout_seconds,
            max_event_bytes=self.config.max_event_bytes,
            max_result_bytes=self.config.max_result_bytes,
            network=network,
        )

    def _run_agent_guarded(
        self, run_id: str, request: AgentRequest, expected_event: str
    ) -> tuple[AgentResult, str | None]:
        before = self.store.capture(run_id)
        result = self.runner.run(request)
        after = self.store.capture(run_id)
        expected = dict(before)
        if expected_event in after:
            expected[expected_event] = after[expected_event]
        unexpected = _changed_keys(expected, after)
        event_missing = expected_event not in after
        if unexpected or event_missing:
            self.store.restore(run_id, expected)
            detail = ", ".join(sorted(unexpected)) or expected_event
            return result, f"Codex changed controller-owned run metadata: {detail}"
        return result, None

    def _metadata_error(
        self, run_id: str, before: dict[str, bytes], expected_event: str
    ) -> str | None:
        after = self.store.capture(run_id)
        expected = dict(before)
        if expected_event in after:
            expected[expected_event] = after[expected_event]
        unexpected = _changed_keys(expected, after)
        if expected_event not in after:
            unexpected.add(expected_event)
        if not unexpected:
            return None
        self.store.restore(run_id, expected)
        return "Codex changed controller-owned run metadata: " + ", ".join(
            sorted(unexpected)
        )

    def _write_agent_evidence(
        self,
        run_id: str,
        name: str,
        request: AgentRequest,
        result: AgentResult,
        *,
        changed_files: list[str] | None = None,
    ) -> None:
        self.store.write_json(
            run_id,
            name,
            {
                "exit_code": result.exit_code,
                "terminal_event": result.terminal_event,
                "timed_out": result.timed_out,
                "malformed_events": result.malformed_events,
                "event_log_truncated": result.event_log_truncated,
                "result_truncated": result.result_truncated,
                "model": request.model,
                "reasoning_effort": request.reasoning_effort,
                "network": request.network,
                "usage": result.usage,
                "stderr": result.stderr,
                "payload": result.payload,
                "changed_files": changed_files or [],
            },
        )

    def _plan(self, run_id: str) -> Plan:
        return Plan.from_dict(self.store.read_json(run_id, "plan.json"))

    def _write_state(self, state: Mapping[str, object]) -> None:
        run_id = state.get("run_id")
        if not isinstance(run_id, str):
            raise ValidationError("state.run_id must be a string")
        self.store.write_json(run_id, "state.json", state)

    @staticmethod
    def _assert_plan_hash(state: Mapping[str, object], plan: Plan) -> None:
        if state.get("plan_sha256") != plan.sha256():
            raise HarnessError("approved plan changed; create a new run")

    def _fail_task(
        self,
        state: dict[str, object],
        task_state: dict[str, object],
        error: str,
        snapshot: GitSnapshot,
        *,
        safety_violation: bool = False,
    ) -> None:
        task_state.update(status="failed", error=error)
        state.update(
            status="failed",
            current_task=task_state["id"],
            failure_stage="task",
            error=error,
            safety_violation=safety_violation,
            workspace_snapshot=snapshot.to_dict(),
            updated_at=now_iso(),
        )
        self._write_state(state)
        self.store.append_event(
            str(state["run_id"]),
            {"type": "task.failed", "task": task_state["id"], "error": error},
        )

    def _block_task(
        self,
        state: dict[str, object],
        task_state: dict[str, object],
        report: TaskReport,
        snapshot: GitSnapshot,
    ) -> None:
        task_state.update(status="blocked", error=report.blocked_reason)
        state.update(
            status="blocked",
            current_task=task_state["id"],
            failure_stage="task",
            error=None,
            blocked_reason=report.blocked_reason,
            required_action=report.required_action,
            safety_violation=False,
            workspace_snapshot=snapshot.to_dict(),
            updated_at=now_iso(),
        )
        self._write_state(state)
        self.store.append_event(
            str(state["run_id"]),
            {
                "type": "task.blocked",
                "task": task_state["id"],
                "reason": report.blocked_reason,
                "required_action": report.required_action,
            },
        )


def _run_id(goal: str) -> str:
    slug = "".join(
        character if character.isascii() and character.isalnum() else "-"
        for character in goal.lower()
    )
    slug = "-".join(filter(None, slug.split("-")))[:24] or "task"
    timestamp = datetime.now(UTC).strftime("%Y%m%dt%H%M%S%f")
    return f"run-{timestamp}-{secrets.token_hex(3)}-{slug}"


def _new_state(run_id: str, plan: Plan) -> dict[str, object]:
    now = now_iso()
    return {
        "version": 1,
        "run_id": run_id,
        "goal": plan.goal,
        "status": "draft",
        "plan_sha256": None,
        "current_task": None,
        "failure_stage": None,
        "error": None,
        "blocked_reason": None,
        "required_action": None,
        "safety_violation": False,
        "workspace_snapshot": None,
        "usage": {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        },
        "tasks": [
            {
                "id": task.id,
                "status": "pending",
                "attempts": 0,
                "retry_profile": False,
                "summary": None,
                "error": None,
                "changed_files": [],
                "candidate_report": None,
                "baseline_snapshot": None,
                "attempt_snapshot": None,
            }
            for task in plan.tasks
        ],
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }


def _validate_state(state: Mapping[str, object], run_id: str) -> None:
    if state.get("version") != 1 or state.get("run_id") != run_id:
        raise ValidationError("invalid run state identity")
    if state.get("status") not in {
        "draft",
        "approved",
        "running",
        "verifying",
        "completed",
        "failed",
        "blocked",
    }:
        raise ValidationError("invalid run status")
    tasks = state.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValidationError("state.tasks must not be empty")


def _task_state(state: Mapping[str, object], task_id: str) -> dict[str, object]:
    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        raise ValidationError("state.tasks must be an array")
    for task in tasks:
        if isinstance(task, dict) and task.get("id") == task_id:
            return task
    raise ValidationError(f"state does not contain {task_id}")


def _task_by_id(plan: Plan, task_id: str) -> Task:
    for task in plan.tasks:
        if task.id == task_id:
            return task
    raise HarnessError(f"plan does not contain task: {task_id}")


def _usage(state: dict[str, object]) -> dict[str, int]:
    usage = state.get("usage")
    if not isinstance(usage, dict):
        raise ValidationError("state.usage must be an object")
    if not all(
        isinstance(key, str) and isinstance(value, int) for key, value in usage.items()
    ):
        raise ValidationError("state.usage values must be integers")
    return usage


def _attempts(task_state: Mapping[str, object]) -> int:
    attempts = task_state.get("attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        raise ValidationError("task state attempts must be an integer")
    return attempts


def _agent_failure(result: AgentResult) -> str:
    return agent_failure(
        exit_code=result.exit_code,
        stderr=result.stderr,
        timed_out=result.timed_out,
        terminal_event=result.terminal_event,
        malformed_events=result.malformed_events,
        result_truncated=result.result_truncated,
    )


def _handoff(
    task: Task,
    task_state: Mapping[str, object],
    report: TaskReport,
    verification: VerificationResult,
) -> dict[str, object]:
    return {
        "version": 1,
        "task": task.id,
        "summary": _bounded(report.summary, 1_000),
        "changed_files": _changed_files(task_state),
        "public_contracts": [
            _bounded(item, 500) for item in report.public_contracts[:10]
        ],
        "decisions": [_bounded(item, 500) for item in report.decisions[:10]],
        "verification": [
            {"argv": list(command.argv), "exit_code": command.exit_code}
            for command in verification.commands
        ],
        "remaining_risks": [
            _bounded(item, 500) for item in report.remaining_risks[:10]
        ],
    }


def _changed_files(task_state: Mapping[str, object]) -> list[str]:
    value = task_state.get("changed_files")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError("task state changed_files must be a string array")
    return value[:100]


def _bounded(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _changed_keys(before: Mapping[str, bytes], after: Mapping[str, bytes]) -> set[str]:
    return {
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    }
