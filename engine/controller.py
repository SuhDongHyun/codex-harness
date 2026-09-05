from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .config import HarnessConfig, ModelProfile
from .context import bounded_text, build_handoff
from .errors import HarnessError, ValidationError
from .git_guard import GitGuard, GitSnapshot, paths_outside_allowed
from .models import Plan, Task, TaskReport, add_usage
from .prompts import agent_failure, planning_prompt, task_prompt
from .runner import AgentRequest, AgentResult, AgentRunner
from .state import RunState, TaskState, new_state, parse_state, task_state
from .store import RunStore, now_iso
from .verifier import VerificationRunner


@dataclass(frozen=True)
class _AttemptContext:
    number: int
    before: GitSnapshot
    request: AgentRequest


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
        last_error: str | None = None
        usage: dict[str, int] = {}
        for attempt in range(1, self.config.max_attempts + 1):
            before_git = self.git.snapshot()
            before_files = self.store.capture(run_id)
            event_name = (
                "evidence/plan-events.jsonl"
                if attempt == 1
                else f"evidence/plan-attempt-{attempt:02d}-events.jsonl"
            )
            agent_name = (
                "evidence/plan-agent.json"
                if attempt == 1
                else f"evidence/plan-attempt-{attempt:02d}-agent.json"
            )
            profile = self.config.planner if attempt == 1 else self.config.retry
            request = AgentRequest(
                prompt=planning_prompt(clean_goal, last_error),
                cwd=self.root,
                sandbox="read-only",
                schema=self.plan_schema,
                event_log=self.store.path(run_id, event_name),
                model=profile.model,
                reasoning_effort=profile.reasoning_effort,
                timeout_seconds=self.config.agent_timeout_seconds,
                max_event_bytes=self.config.max_event_bytes,
                max_result_bytes=self.config.max_result_bytes,
            )
            result = self.runner.run(request)
            add_usage(usage, result.usage)
            after_git = self.git.snapshot()
            metadata_error = self._metadata_error(run_id, before_files, event_name)
            self._write_agent_evidence(run_id, agent_name, request, result)
            if before_git.fingerprint != after_git.fingerprint:
                raise HarnessError("planner changed the project Git working tree")
            if metadata_error:
                raise HarnessError(metadata_error)
            plan: Plan | None = None
            if result.succeeded:
                try:
                    plan = self._materialize_plan(result.payload)
                    self._assert_plan_approvable(plan, clean_goal)
                except (HarnessError, ValidationError) as error:
                    plan = None
                    last_error = f"planner returned an invalid plan: {error}"
            else:
                last_error = _agent_failure(result)
            if plan is not None:
                self.store.write_json(run_id, "plan.json", plan.to_dict())
                state = new_state(run_id, plan)
                add_usage(state["usage"], usage)
                self._write_state(state)
                self.store.append_event(
                    run_id,
                    {
                        "type": "plan.created",
                        "attempt": attempt,
                        "model": request.model,
                        "reasoning_effort": request.reasoning_effort,
                        "usage": usage,
                    },
                )
                return run_id
            if attempt < self.config.max_attempts:
                if last_error is None:
                    raise AssertionError("rejected plan requires failure evidence")
                last_error = bounded_text(
                    last_error, self.config.max_retry_context_bytes
                )
                self.store.append_event(
                    run_id,
                    {
                        "type": "plan.retrying",
                        "attempt": attempt,
                        "error": last_error,
                    },
                )
        raise HarnessError(
            f"planner failed after {self.config.max_attempts} attempts: {last_error}"
        )

    def approve(self, run_id: str) -> RunState:
        with self.store.lock(run_id):
            state = self.status(run_id)
            if state["status"] != "draft":
                raise HarnessError("only a draft run can be approved")
            plan = self._plan(run_id)
            self._assert_context_sources_current(plan)
            self._assert_plan_approvable(plan, state["goal"])
            snapshot = self.git.snapshot()
            state["status"] = "approved"
            state["plan_sha256"] = plan.sha256()
            state["workspace_snapshot"] = snapshot.to_dict()
            state["updated_at"] = now_iso()
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

    def _assert_plan_approvable(self, plan: Plan, goal: object) -> None:
        if not isinstance(goal, str) or plan.goal != goal:
            raise HarnessError("planner changed the requested goal")
        self._assert_context_sources_read_only(plan)
        self._assert_engine_protected(plan)
        if (
            any(task.network for task in plan.tasks)
            and not self.config.executor_network
        ):
            requested = ", ".join(task.id for task in plan.tasks if task.network)
            raise HarnessError(
                f"tasks request network ({requested}) but executor_network is false"
            )

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

    def run(self, run_id: str) -> RunState:
        with self.store.lock(run_id):
            state = self.status(run_id)
            plan = self._plan(run_id)
            if state["status"] != "approved":
                raise HarnessError("run requires approved state")
            self._assert_plan_hash(state, plan)
            self._assert_context_sources_current(plan)
            expected = GitSnapshot.from_dict(state["workspace_snapshot"])
            current = self.git.snapshot()
            if expected.fingerprint != current.fingerprint:
                raise HarnessError("project changed after approval")
            state["status"] = "running"
            state["updated_at"] = now_iso()
            self._write_state(state)
            self.store.append_event(run_id, {"type": "run.started"})
            return self._execute(run_id, state, plan)

    def resume(self, run_id: str) -> RunState:
        with self.store.lock(run_id):
            state = self.status(run_id)
            plan = self._plan(run_id)
            if state["status"] not in {"running", "verifying"}:
                raise HarnessError("resume requires an interrupted running run")
            self._assert_plan_hash(state, plan)
            self._assert_context_sources_current(plan)
            current_task_id = state.get("current_task")
            if isinstance(current_task_id, str):
                task = _task_by_id(plan, current_task_id)
                current_state = task_state(state, current_task_id)
                raw_before = current_state.get("attempt_snapshot") or current_state.get(
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
                if current_state["status"] == "running":
                    current_state["status"] = "retrying"
                    current_state["error"] = (
                        "controller process was interrupted during execution"
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

    def retry_task(self, run_id: str, target: str) -> RunState:
        with self.store.lock(run_id):
            state = self.status(run_id)
            plan = self._plan(run_id)
            if state["status"] not in {"failed", "blocked"}:
                raise HarnessError("retry-task requires a failed or blocked run")
            self._assert_plan_hash(state, plan)
            self._assert_context_sources_current(plan)
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
                current_state = task_state(state, target)
                if current_state["status"] not in {"failed", "blocked"}:
                    raise HarnessError(f"{target} is not failed or blocked")
                allowed = task.write_paths
                current_state["status"] = "pending"
                current_state["attempts"] = 0
                current_state["retry_profile"] = True
                current_state["error"] = None
                current_state["summary"] = None
                current_state["candidate_report"] = None
                current_state["baseline_snapshot"] = None
                current_state["attempt_snapshot"] = None
                current_state["changed_files"] = []
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
            state["status"] = "approved"
            state["current_task"] = None
            state["failure_stage"] = None
            state["error"] = None
            state["blocked_reason"] = None
            state["required_action"] = None
            state["safety_violation"] = False
            state["workspace_snapshot"] = current.to_dict()
            state["updated_at"] = now_iso()
            self._write_state(state)
            self.store.append_event(
                run_id, {"type": "run.retry_approved", "target": target}
            )
            return state

    def status(self, run_id: str) -> RunState:
        state = self.store.read_json(run_id, "state.json")
        return parse_state(state, run_id)

    def _execute(self, run_id: str, state: RunState, plan: Plan) -> RunState:
        for index, task in enumerate(plan.tasks):
            current_state = task_state(state, task.id)
            if current_state["status"] == "completed":
                continue
            if current_state["status"] == "verifying":
                outcome = self._verify_task(run_id, state, task, current_state)
                if outcome == "completed":
                    continue
                if outcome == "terminal":
                    return state
            terminal = self._run_task(
                run_id,
                state,
                plan,
                task,
                current_state,
                previous_task=plan.tasks[index - 1] if index else None,
            )
            if terminal:
                return state
        return self._finalize(run_id, state, plan)

    def _run_task(
        self,
        run_id: str,
        state: RunState,
        plan: Plan,
        task: Task,
        current_state: TaskState,
        *,
        previous_task: Task | None,
    ) -> bool:
        last_error = current_state["error"]
        if current_state["attempts"] >= self.config.max_attempts:
            self._fail_task(
                state,
                current_state,
                last_error or "interrupted task exhausted its attempt limit",
                self.git.snapshot(),
            )
            return True
        while current_state["attempts"] < self.config.max_attempts:
            context = self._prepare_attempt(
                run_id,
                state,
                plan,
                task,
                current_state,
                previous_task,
                last_error,
            )
            result, after, safety_error = self._run_attempt(
                run_id, state, task, context
            )
            outcome, last_error = self._evaluate_attempt(
                run_id,
                state,
                task,
                current_state,
                context,
                result,
                after,
                safety_error,
            )
            if outcome == "completed":
                return False
            if outcome == "terminal":
                return True
            if context.number >= self.config.max_attempts:
                self._fail_task(
                    state,
                    current_state,
                    last_error or "task failed without evidence",
                    after,
                )
                return True
            current_state["status"] = "retrying"
            current_state["error"] = last_error
            state["updated_at"] = now_iso()
            self._write_state(state)
            self.store.append_event(
                run_id,
                {
                    "type": "task.retrying",
                    "task": task.id,
                    "attempt": context.number,
                    "error": last_error,
                },
            )
        return True

    def _prepare_attempt(
        self,
        run_id: str,
        state: RunState,
        plan: Plan,
        task: Task,
        current_state: TaskState,
        previous_task: Task | None,
        last_error: str | None,
    ) -> _AttemptContext:
        if current_state["baseline_snapshot"] is None:
            current_state["baseline_snapshot"] = self.git.snapshot().to_dict()
        before = self.git.snapshot()
        attempt = current_state["attempts"] + 1
        current_state["status"] = "running"
        current_state["attempts"] = attempt
        current_state["error"] = None
        current_state["attempt_snapshot"] = before.to_dict()
        state["status"] = "running"
        state["current_task"] = task.id
        state["updated_at"] = now_iso()
        self._write_state(state)
        profile = (
            self.config.retry
            if attempt > 1 or current_state["retry_profile"]
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
        return _AttemptContext(attempt, before, request)

    def _run_attempt(
        self,
        run_id: str,
        state: RunState,
        task: Task,
        context: _AttemptContext,
    ) -> tuple[AgentResult, GitSnapshot, str | None]:
        result, metadata_error = self._run_agent_guarded(
            run_id,
            context.request,
            f"evidence/{task.id}-attempt-{context.number:02d}.jsonl",
        )
        after = self.git.snapshot()
        add_usage(state["usage"], result.usage)
        self._write_agent_evidence(
            run_id,
            f"evidence/{task.id}-attempt-{context.number:02d}-agent.json",
            context.request,
            result,
            changed_files=sorted(self.git.changed_paths(context.before, after)),
        )
        safety_error = metadata_error or self.git.safety_error(
            context.before, after, task.write_paths
        )
        return result, after, safety_error

    def _evaluate_attempt(
        self,
        run_id: str,
        state: RunState,
        task: Task,
        current_state: TaskState,
        context: _AttemptContext,
        result: AgentResult,
        after: GitSnapshot,
        safety_error: str | None,
    ) -> tuple[str, str | None]:
        if safety_error:
            self._fail_task(
                state,
                current_state,
                safety_error,
                context.before,
                safety_violation=True,
            )
            return "terminal", safety_error
        if not result.succeeded:
            return "retry", _agent_failure(result)
        try:
            report = TaskReport.from_dict(result.payload)
        except ValidationError as validation_error:
            return "retry", f"invalid task report: {validation_error}"
        if report.outcome == "blocked":
            self._block_task(state, current_state, report, after)
            return "terminal", report.blocked_reason
        if report.outcome == "failed":
            return "retry", report.error or "Codex reported failure"
        baseline = GitSnapshot.from_dict(current_state["baseline_snapshot"])
        current_state["status"] = "verifying"
        current_state["candidate_report"] = report.to_dict()
        current_state["changed_files"] = sorted(self.git.changed_paths(baseline, after))
        self._write_state(state)
        outcome = self._verify_task(run_id, state, task, current_state)
        error = current_state["error"] or "verification failed"
        return outcome, error if outcome == "retry" else None

    def _verify_task(
        self,
        run_id: str,
        state: RunState,
        task: Task,
        current_state: TaskState,
    ) -> str:
        attempt = current_state["attempts"]
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
                current_state,
                safety_error,
                before,
                safety_violation=True,
            )
            return "terminal"
        if not result.ok:
            current_state["status"] = "retrying"
            current_state["error"] = result.failure_summary()
            self._write_state(state)
            if attempt >= self.config.max_attempts:
                self._fail_task(state, current_state, result.failure_summary(), after)
                return "terminal"
            return "retry"
        report = TaskReport.from_dict(current_state["candidate_report"])
        handoff = build_handoff(
            task,
            current_state,
            report,
            result,
            self.config.max_handoff_bytes,
        )
        self.store.write_json(run_id, f"handoffs/{task.id}.json", handoff)
        current_state["status"] = "completed"
        current_state["retry_profile"] = False
        current_state["summary"] = report.summary
        current_state["error"] = None
        current_state["candidate_report"] = None
        current_state["baseline_snapshot"] = None
        current_state["attempt_snapshot"] = None
        state["current_task"] = None
        state["workspace_snapshot"] = after.to_dict()
        state["updated_at"] = now_iso()
        self._write_state(state)
        self.store.append_event(
            run_id,
            {"type": "task.completed", "task": task.id, "attempt": attempt},
        )
        return "completed"

    def _finalize(self, run_id: str, state: RunState, plan: Plan) -> RunState:
        state["status"] = "verifying"
        state["current_task"] = None
        state["failure_stage"] = "final"
        state["updated_at"] = now_iso()
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
            state["status"] = "failed"
            state["failure_stage"] = "final"
            state["error"] = error
            state["safety_violation"] = safety_violation
            state["workspace_snapshot"] = (
                before if safety_violation else after
            ).to_dict()
            state["updated_at"] = now_iso()
            self._write_state(state)
            self.store.append_event(
                run_id, {"type": "run.failed", "stage": "final", "error": error}
            )
            return state
        state["status"] = "completed"
        state["current_task"] = None
        state["failure_stage"] = None
        state["error"] = None
        state["workspace_snapshot"] = after.to_dict()
        state["updated_at"] = now_iso()
        state["completed_at"] = now_iso()
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
                last_error=(
                    bounded_text(last_error, self.config.max_retry_context_bytes)
                    if last_error is not None
                    else None
                ),
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

    def _materialize_plan(self, payload: object) -> Plan:
        if not isinstance(payload, Mapping):
            raise ValidationError("plan must be an object")
        raw = dict(payload)
        sources = raw.get("context_sources")
        if not isinstance(sources, list) or not all(
            isinstance(path, str) for path in sources
        ):
            raise ValidationError(
                "planner context_sources must be an array of repository paths"
            )
        raw["context_sources"] = [
            {"path": path, "sha256": self._context_source_sha256(path)}
            for path in sources
        ]
        return Plan.from_dict(raw)

    def _context_source_sha256(self, relative: str) -> str:
        pure = PurePosixPath(relative)
        if (
            not relative
            or pure.is_absolute()
            or ".." in pure.parts
            or "\\" in relative
            or any(character in relative for character in "*?[{")
        ):
            raise ValidationError(f"unsafe context source path: {relative!r}")
        candidate = self.root.joinpath(*pure.parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as error:
            raise ValidationError(
                f"context source is missing or outside the project: {relative}"
            ) from error
        if not resolved.is_file():
            raise ValidationError(f"context source is not a file: {relative}")
        digest = hashlib.sha256()
        try:
            with resolved.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            raise ValidationError(f"cannot read context source: {relative}") from error
        return digest.hexdigest()

    def _assert_context_sources_current(self, plan: Plan) -> None:
        for source in plan.context_sources:
            try:
                current = self._context_source_sha256(source.path)
            except ValidationError as error:
                raise HarnessError(
                    "planning context changed; create a new run: " + source.path
                ) from error
            if current != source.sha256:
                raise HarnessError(
                    "planning context changed; create a new run: " + source.path
                )

    @staticmethod
    def _assert_context_sources_read_only(plan: Plan) -> None:
        write_paths = tuple(path for task in plan.tasks for path in task.write_paths)
        writable = [
            source.path
            for source in plan.context_sources
            if not paths_outside_allowed((source.path,), write_paths)
        ]
        if writable:
            raise HarnessError(
                "context sources overlap task write_paths: " + ", ".join(writable)
            )

    def _write_state(self, state: RunState) -> None:
        self.store.write_json(state["run_id"], "state.json", state)

    @staticmethod
    def _assert_plan_hash(state: Mapping[str, object], plan: Plan) -> None:
        if state.get("plan_sha256") != plan.sha256():
            raise HarnessError("approved plan changed; create a new run")

    def _fail_task(
        self,
        state: RunState,
        current_state: TaskState,
        error: str,
        snapshot: GitSnapshot,
        *,
        safety_violation: bool = False,
    ) -> None:
        current_state["status"] = "failed"
        current_state["error"] = error
        state["status"] = "failed"
        state["current_task"] = current_state["id"]
        state["failure_stage"] = "task"
        state["error"] = error
        state["safety_violation"] = safety_violation
        state["workspace_snapshot"] = snapshot.to_dict()
        state["updated_at"] = now_iso()
        self._write_state(state)
        self.store.append_event(
            str(state["run_id"]),
            {"type": "task.failed", "task": current_state["id"], "error": error},
        )

    def _block_task(
        self,
        state: RunState,
        current_state: TaskState,
        report: TaskReport,
        snapshot: GitSnapshot,
    ) -> None:
        current_state["status"] = "blocked"
        current_state["error"] = report.blocked_reason
        state["status"] = "blocked"
        state["current_task"] = current_state["id"]
        state["failure_stage"] = "task"
        state["error"] = None
        state["blocked_reason"] = report.blocked_reason
        state["required_action"] = report.required_action
        state["safety_violation"] = False
        state["workspace_snapshot"] = snapshot.to_dict()
        state["updated_at"] = now_iso()
        self._write_state(state)
        self.store.append_event(
            str(state["run_id"]),
            {
                "type": "task.blocked",
                "task": current_state["id"],
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


def _task_by_id(plan: Plan, task_id: str) -> Task:
    for task in plan.tasks:
        if task.id == task_id:
            return task
    raise HarnessError(f"plan does not contain task: {task_id}")


def _agent_failure(result: AgentResult) -> str:
    return agent_failure(
        exit_code=result.exit_code,
        stderr=result.stderr,
        timed_out=result.timed_out,
        terminal_event=result.terminal_event,
        malformed_events=result.malformed_events,
        result_truncated=result.result_truncated,
    )


def _changed_keys(before: Mapping[str, bytes], after: Mapping[str, bytes]) -> set[str]:
    return {
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    }
