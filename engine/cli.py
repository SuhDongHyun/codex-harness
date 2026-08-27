from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import HarnessConfig
from .controller import HarnessController
from .errors import GitError, HarnessError, ValidationError
from .git_guard import GitGuard
from .init_project import initialize_project
from .runner import CodexRunner
from .store import RunStore
from .verifier import Verifier

ENGINE_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Run ordered coding tasks in fresh Codex sessions.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="target Git repository (defaults to current directory)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create parent-project integration files")
    init.add_argument("--force", action="store_true")
    plan = commands.add_parser("plan", help="create a read-only draft plan")
    plan.add_argument("goal", nargs="+")
    approve = commands.add_parser("approve", help="approve one exact draft run")
    approve.add_argument("run_id")
    run = commands.add_parser("run", help="execute an approved run")
    run.add_argument("run_id")
    resume = commands.add_parser("resume", help="recover an interrupted run")
    resume.add_argument("run_id")
    retry = commands.add_parser(
        "retry-task", help="reopen one failed/blocked task or final verification"
    )
    retry.add_argument("run_id")
    retry.add_argument("task_id")
    status = commands.add_parser("status", help="print controller-owned state")
    status.add_argument("run_id")
    commands.add_parser("version", help="print harness version")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.expanduser().resolve()
    try:
        if args.command == "version":
            _print({"version": __version__, "engine_root": str(ENGINE_ROOT)})
            return 0
        git = GitGuard(project_root)
        git.assert_repository()
        if args.command == "init":
            changed = initialize_project(
                project_root,
                ENGINE_ROOT,
                force=args.force,
            )
            _print({"project_root": str(project_root), "changed": changed})
            return 0
        controller = build_controller(project_root, git)
        if args.command == "plan":
            run_id = controller.plan(" ".join(args.goal))
            _print({"run_id": run_id, "status": "draft"})
            return 0
        if args.command == "approve":
            _print(controller.approve(args.run_id))
            return 0
        if args.command == "status":
            _print(controller.status(args.run_id))
            return 0
        if args.command == "resume":
            state = controller.resume(args.run_id)
        elif args.command == "retry-task":
            state = controller.retry_task(args.run_id, args.task_id)
        else:
            state = controller.run(args.run_id)
        _print(state)
        if state["status"] in {"completed", "approved"}:
            return 0
        return 2 if state["status"] == "blocked" else 1
    except (HarnessError, ValidationError, GitError, OSError) as error:
        print(f"harness: {error}", file=sys.stderr)
        return 2


def build_controller(
    project_root: Path, git: GitGuard | None = None
) -> HarnessController:
    config = HarnessConfig.load(project_root / ".harness" / "config.toml")
    return HarnessController(
        project_root=project_root,
        config=config,
        store=RunStore(project_root / ".harness" / "runs"),
        runner=CodexRunner(config.codex_command),
        verifier=Verifier(
            config.codex_command,
            config.verification_timeout_seconds,
            config.max_verification_bytes,
            sandboxed=config.sandbox_verification,
        ),
        git_guard=git or GitGuard(project_root),
    )


def entrypoint() -> None:
    raise SystemExit(main())


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))
