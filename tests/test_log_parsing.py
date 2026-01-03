import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import telegram_stubs

telegram_stubs.install()

import vibes  # noqa: E402


class LogParsingTests(unittest.TestCase):
    def test_extract_last_agent_message_from_stdout_log(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "stdout.jsonl"
            lines = [
                "not-json\n",
                json.dumps({"type": "assistant_message", "text": "first"}) + "\n",
                json.dumps({"type": "item.created", "item": {"type": "assistant_message", "text": "second"}}) + "\n",
            ]
            path.write_text("".join(lines), encoding="utf-8")

            msg = vibes._extract_last_agent_message_from_stdout_log(str(path), max_chars=200)
            self.assertEqual(msg, "second")

    def test_extract_last_agent_message_from_stdout_log_claude(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "stdout.jsonl"
            events = [
                {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "Final answer"}]}},
            ]
            path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

            msg = vibes._extract_last_agent_message_from_stdout_log(str(path), max_chars=200)
            self.assertEqual(msg, "Final answer")

    def test_preview_from_stdout_log_includes_key_events(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "stdout.jsonl"
            events = [
                {"type": "item.created", "item": {"type": "reasoning", "text": "SHOULD_NOT_LEAK"}},
                {
                    "type": "item.command_execution.started",
                    "item": {"type": "command_execution", "command": "ls", "status": "in_progress"},
                },
                {
                    "type": "item.command_execution.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "ls",
                        "status": "completed",
                        "aggregated_output": "file1\nfile2\n",
                        "exit_code": 0,
                    },
                },
                {"type": "tool_use", "input": {"command": "echo hi"}},
                {"type": "tool_result", "output": "hi"},
                {"type": "file_change", "diff": "*** Begin Patch\\n*** End Patch"},
                {"type": "text", "delta": "streamed"},
                {"type": "assistant_message", "text": "done"},
            ]
            path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

            preview = vibes._preview_from_stdout_log(str(path), max_chars=2000)
            self.assertIn("$ ls", preview)
            self.assertIn("file1", preview)
            self.assertIn("exit_code", preview)
            self.assertIn("[tool_use]", preview)
            self.assertIn("echo hi", preview)
            self.assertIn("[tool_result]", preview)
            self.assertIn("[file_change]", preview)
            self.assertIn("streamed", preview)
            self.assertIn("done", preview)
            self.assertNotIn("SHOULD_NOT_LEAK", preview)

    def test_preview_from_stdout_log_claude_delta(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "stdout.jsonl"
            events = [
                {"type": "system", "subtype": "init", "session_id": "abc"},
                {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}},
                {"type": "result", "result": "Hi"},
            ]
            path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

            preview = vibes._preview_from_stdout_log(str(path), max_chars=2000)
            self.assertIn("Hi", preview)

    def test_preview_from_stderr_log_returns_tail(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "stderr.txt"
            path.write_text("".join(f"line {i}\n" for i in range(100)), encoding="utf-8")

            preview = vibes._preview_from_stderr_log(str(path), max_chars=5000)
            self.assertIn("line 99", preview)
            self.assertNotIn("line 0", preview)
