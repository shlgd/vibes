import asyncio
import unittest
from collections import deque
from pathlib import Path

import telegram_stubs

telegram_stubs.install()

import vibes  # noqa: E402


class _FakeProcess:
    returncode = None


class _CapturingStream:
    def __init__(self) -> None:
        self.headers: list[tuple[str, int | None]] = []
        self.markups: list[object | None] = []

    async def set_header(self, *, header_html: str, header_plain_len: int | None = None) -> None:
        self.headers.append((header_html, header_plain_len))

    async def set_reply_markup(self, reply_markup: object | None) -> None:
        self.markups.append(reply_markup)


class StopFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        # Ensure we don't leak tasks from this module.
        await asyncio.sleep(0)

    async def test_show_and_restore_stop_confirmation(self) -> None:
        rec = vibes.SessionRecord(name="S", path=".")
        rec.status = "running"

        stream = _CapturingStream()
        run = vibes.SessionRun(
            process=_FakeProcess(),  # type: ignore[arg-type]
            stdout_task=asyncio.create_task(asyncio.sleep(0)),
            stderr_task=asyncio.create_task(asyncio.sleep(0)),
            stream=stream,  # type: ignore[arg-type]
            stdout_log=Path("stdout.jsonl"),
            stderr_log=Path("stderr.txt"),
            stderr_tail=deque(),
        )
        rec.run = run

        await vibes._show_stop_confirmation_in_stream(rec)

        self.assertTrue(rec.run.confirm_stop)
        self.assertEqual(rec.run.header_note, vibes._STOP_CONFIRM_QUESTION)
        self.assertTrue(stream.headers)
        self.assertIn(vibes._STOP_CONFIRM_QUESTION, stream.headers[-1][0])

        confirm_markup = stream.markups[-1]
        buttons = getattr(confirm_markup, "inline_keyboard", [])
        texts = [getattr(btn, "text", "") for row in buttons for btn in (row or [])]
        self.assertIn("✅ Yes, stop", texts)
        self.assertIn("❌ No", texts)

        await vibes._restore_run_stream_ui(rec)

        self.assertFalse(rec.run.confirm_stop)
        self.assertIsNone(rec.run.header_note)
        detach_markup = stream.markups[-1]
        detach_buttons = getattr(detach_markup, "inline_keyboard", [])
        detach_callback_data = [
            getattr(btn, "callback_data", None) for row in detach_buttons for btn in (row or [])
        ]
        self.assertIn(vibes._cb("detach"), detach_callback_data)

