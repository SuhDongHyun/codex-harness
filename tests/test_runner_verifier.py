from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from engine.runner import AgentRequest, CodexRunner
from engine.verifier import Verifier


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
        self.assertIn("harness-verification", command)
        self.assertIn('permissions.harness-verification.extends=":workspace"', command)
        self.assertIn("permissions.harness-verification.network.enabled=true", command)
        self.assertIn("features.network_proxy.enabled=true", command)
        self.assertIn(
            'features.network_proxy.domains={"localhost"="allow","127.0.0.1"="allow"}',
            command,
        )
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

    def test_unittest_skip_is_not_accepted_as_verification(self) -> None:
        verifier = Verifier("unused", 10, 4096, sandboxed=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_skip.py").write_text(
                "import unittest\n"
                "\n"
                "class SkipTest(unittest.TestCase):\n"
                "    @unittest.skip('missing required capability')\n"
                "    def test_required_behavior(self):\n"
                "        pass\n",
                encoding="utf-8",
            )
            result = verifier.verify(
                (("python3", "-m", "unittest", "-v", "test_skip"),), root
            )

        self.assertFalse(result.ok)
        self.assertTrue(result.commands[0].skipped_tests)
        self.assertIn("reported skipped tests", result.failure_summary())


if __name__ == "__main__":
    unittest.main()
