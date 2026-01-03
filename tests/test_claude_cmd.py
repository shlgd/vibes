import unittest

import telegram_stubs

telegram_stubs.install()

import vibes  # noqa: E402


class ClaudeCmdTests(unittest.TestCase):
    def test_build_claude_cmd_includes_stream_json_and_resume(self) -> None:
        class _BareManager(vibes.SessionManager):
            def _load_state(self) -> None:  # pragma: no cover
                return None

        manager = _BareManager(admin_id=None)
        rec = vibes.SessionRecord(
            name="S",
            path=".",
            engine=vibes.ENGINE_CLAUDE,
            thread_id="03a97da8-27b5-4b56-aa1f-b3231ef42f10",
            model="sonnet",
        )

        cmd = manager._build_claude_cmd(rec, prompt="hello", run_mode="continue")

        self.assertEqual(cmd[0], "claude")
        self.assertIn("-p", cmd)
        self.assertIn("--verbose", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--include-partial-messages", cmd)
        self.assertIn("--permission-mode", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("-r", cmd)
