from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_harness.runner import AgentRequest, CodexRunner
from codex_harness.verifier import Verifier


class RunnerVerifierTests(unittest.TestCase):
    def test_codex_command_uses_fresh_ephemeral_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = AgentRequest(
                prompt="do one task",
                cwd=root,
                sandbox="workspace-write",
                schema=root / "schema.json",
                event_log=root / "event.jsonl",
                model="test-model",
                reasoning_effort="high",
                timeout_seconds=10,
                max_event_bytes=1024,
                max_result_bytes=1024,
                network=False,
            )
            command = CodexRunner("codex-test").build_command(
                request, root / "result.json"
            )

        self.assertEqual(command[:2], ["codex-test", "exec"])
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertNotIn("resume", command)
        self.assertIn("agents.enabled=false", command)
        self.assertIn("sandbox_workspace_write.network_access=false", command)

    def test_verifier_runs_argv_without_shell(self) -> None:
        verifier = Verifier("codex-test", 10, 1024, sandboxed=True)
        command = verifier._command(("python3", "-V"), Path("/tmp/project"))

        self.assertEqual(command[:2], ["codex-test", "sandbox"])
        self.assertIn(":workspace", command)
        self.assertEqual(command[-2:], ["python3", "-V"])
        self.assertNotIn("bash", command)

    def test_unsandboxed_verifier_executes_command(self) -> None:
        verifier = Verifier("unused", 10, 1024, sandboxed=False)
        with tempfile.TemporaryDirectory() as directory:
            result = verifier.verify(
                (("python3", "-c", "print('verified')"),), Path(directory)
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.commands[0].stdout, "verified\n")


if __name__ == "__main__":
    unittest.main()
