import asyncio
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory


import telegram_stubs

telegram_stubs.install()

import vibes  # noqa: E402


class _FakeProcess:
    returncode = None


class _FakeStream:
    def __init__(self, *, chat_id: int, message_id: int) -> None:
        self._chat_id = chat_id
        self._message_id = message_id
        self.pause_calls = 0

    def get_chat_id(self) -> int:
        return self._chat_id

    def get_message_id(self) -> int:
        return self._message_id

    async def pause(self) -> None:
        self.pause_calls += 1


class _FakePanelUI:
    def __init__(self, *, fixed_panel_message_id: int) -> None:
        self.fixed_panel_message_id = fixed_panel_message_id
        self.last_text_html: str | None = None
        self.last_reply_markup: object | None = None

    async def ensure_panel(self, chat_id: int) -> int:  # pragma: no cover
        return self.fixed_panel_message_id

    async def render_to_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        text_html: str,
        reply_markup: object,
        update_state_on_replace: bool,
    ) -> int:
        self.last_text_html = text_html
        self.last_reply_markup = reply_markup
        return message_id

    async def delete_message_best_effort(self, *, chat_id: int, message_id: int) -> None:  # pragma: no cover
        return None


class SessionIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []

    async def asyncTearDown(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _task(self) -> asyncio.Task[None]:
        t = asyncio.create_task(asyncio.sleep(3600))
        self._tasks.append(t)
        return t

    def _mk_running_session(self, *, name: str, chat_id: int, message_id: int, paused: bool) -> vibes.SessionRecord:
        rec = vibes.SessionRecord(name=name, path=".")
        rec.status = "running"
        run = vibes.SessionRun(
            process=_FakeProcess(),
            stdout_task=self._task(),
            stderr_task=self._task(),
            stream=_FakeStream(chat_id=chat_id, message_id=message_id),
            stdout_log=Path("stdout.jsonl"),
            stderr_log=Path("stderr.txt"),
            stderr_tail=deque(),
            paused=paused,
        )
        rec.run = run
        return rec

    def test_status_icons_running_and_success(self) -> None:
        running = vibes.SessionRecord(name="A", path=".")
        running.status = "running"
        running.last_result = "never"
        self.assertEqual(vibes._status_emoji(running), "🟢")

        ok = vibes.SessionRecord(name="B", path=".")
        ok.status = "idle"
        ok.last_result = "success"
        self.assertEqual(vibes._status_emoji(ok), "✅")

    def test_session_view_has_no_disconnect_button(self) -> None:
        manager = vibes.SessionManager(admin_id=None)
        rec = vibes.SessionRecord(name="S", path=".")
        manager.sessions = {"S": rec}

        _text, markup = vibes._render_session_view(manager, session_name="S")
        buttons = getattr(markup, "inline_keyboard", [])
        texts = [getattr(btn, "text", "") for row in buttons for btn in (row or [])]
        self.assertNotIn("🔌 Disconnect", texts)
        self.assertIn("⚙️", texts)
        self.assertIn(vibes.LABEL_BACK, texts)
        self.assertIn("🗑", texts)
        self.assertNotIn(vibes.LABEL_LOG, texts)
        self.assertNotIn(vibes.LABEL_START, texts)

    async def test_session_view_running_shows_watch_logs_and_stop(self) -> None:
        chat_id = 100
        message_id = 200
        manager = vibes.SessionManager(admin_id=None)
        rec = self._mk_running_session(name="S", chat_id=chat_id, message_id=message_id, paused=True)
        manager.sessions = {"S": rec}

        _text, markup = vibes._render_session_view(manager, session_name="S")
        buttons = getattr(markup, "inline_keyboard", [])
        texts = [getattr(btn, "text", "") for row in buttons for btn in (row or [])]
        self.assertIn("⬅️", texts)
        self.assertIn("⛔", texts)

    async def test_resolve_attached_running_session_ignores_stale_mapping(self) -> None:
        chat_id = 100
        message_id = 200

        manager = vibes.SessionManager(admin_id=None)
        manager.sessions = {}

        # Scenario that used to break:
        # - Two sessions share the same panel message_id.
        # - The internal mapping points to session B (last run started),
        #   but session A is the one currently attached (unpaused) and editing the panel.
        a = self._mk_running_session(name="A", chat_id=chat_id, message_id=message_id, paused=False)
        b = self._mk_running_session(name="B", chat_id=chat_id, message_id=message_id, paused=True)
        manager.sessions = {"A": a, "B": b}
        manager.register_run_message(chat_id=chat_id, message_id=message_id, session_name="B")

        resolved = manager.resolve_attached_running_session_for_message(chat_id=chat_id, message_id=message_id)
        self.assertEqual(resolved, "A")

    async def test_pause_other_attached_runs_only_pauses_other_unpaused(self) -> None:
        chat_id = 100
        message_id = 200

        manager = vibes.SessionManager(admin_id=None)
        manager.sessions = {}

        a = self._mk_running_session(name="A", chat_id=chat_id, message_id=message_id, paused=False)
        b = self._mk_running_session(name="B", chat_id=chat_id, message_id=message_id, paused=False)
        manager.sessions = {"A": a, "B": b}

        await manager.pause_other_attached_runs(chat_id=chat_id, message_id=message_id, except_session="A")

        self.assertFalse(a.run.paused)
        self.assertTrue(b.run.paused)
        self.assertEqual(getattr(b.run.stream, "pause_calls", -1), 1)

    async def test_render_and_sync_pauses_attached_run_so_logs_ui_is_not_overwritten(self) -> None:
        chat_id = 100
        message_id = 200

        manager = vibes.SessionManager(admin_id=None)
        manager.sessions = {}
        manager.panel_by_chat = {chat_id: message_id}

        a = self._mk_running_session(name="A", chat_id=chat_id, message_id=message_id, paused=False)
        manager.sessions = {"A": a}

        panel = _FakePanelUI(fixed_panel_message_id=message_id)

        class _Ctx:
            def __init__(self) -> None:
                self.chat_data = {"ui": {"mode": "logs", "session": "A"}}
                self.application = object()

        context = _Ctx()

        await vibes._render_and_sync(manager, panel, context=context, chat_id=chat_id)

        self.assertTrue(a.run.paused)
        self.assertEqual(getattr(a.run.stream, "pause_calls", -1), 1)
        self.assertIsNotNone(panel.last_reply_markup)

    async def test_run_prompt_renders_finished_session_screen(self) -> None:
        class _CapturingPanelUI:
            last_instance: "_CapturingPanelUI | None" = None

            def __init__(self, application: object, manager: object) -> None:
                self.renders: list[tuple[int, int, str, object]] = []
                _CapturingPanelUI.last_instance = self

            async def render_to_message(
                self,
                *,
                chat_id: int,
                message_id: int,
                text_html: str,
                reply_markup: object,
                update_state_on_replace: bool,
            ) -> int:
                self.renders.append((chat_id, message_id, text_html, reply_markup))
                return message_id

        class _CapturingStream:
            def __init__(
                self,
                application: object,
                chat_id: int,
                message_id: int,
                *,
                header_html: str = "",
                header_plain_len: int = 0,
                auto_clear_header_on_first_log: bool = False,
                footer_provider: object | None = None,
                footer_plain_len: int = 0,
                wrap_log_in_pre: bool = False,
                reply_markup: object | None = None,
                on_panel_replaced: object | None = None,
            ) -> None:
                self._chat_id = chat_id
                self._message_id = message_id

            def get_chat_id(self) -> int:
                return self._chat_id

            def get_message_id(self) -> int:
                return self._message_id

            async def set_footer(  # pragma: no cover
                self,
                *,
                footer_provider: object | None,
                footer_plain_len: int | None = None,
                wrap_log_in_pre: bool | None = None,
            ) -> None:
                return None

            async def set_reply_markup(self, reply_markup: object | None) -> None:  # pragma: no cover
                return None

            async def pause(self) -> None:  # pragma: no cover
                return None

            async def resume(self) -> None:  # pragma: no cover
                return None

            async def add_text(self, text: str) -> None:  # pragma: no cover
                return None

            async def stop(self) -> None:  # pragma: no cover
                return None

        class _FakeProcess:
            def __init__(self, return_code: int) -> None:
                self.returncode: int | None = None
                self._return_code = return_code

            async def wait(self) -> int:
                self.returncode = self._return_code
                return self._return_code

        class _RunPromptManager(vibes.SessionManager):
            async def save_state(self) -> None:  # pragma: no cover
                return None

            async def _spawn_process(self, cmd: list[str], *, cwd: str | None = None) -> object:
                return _FakeProcess(return_code=0)

            async def _read_stdout(  # pragma: no cover
                self,
                *,
                rec: vibes.SessionRecord,
                process: object,
                stream: object,
                log_path: Path,
            ) -> None:
                return None

            async def _read_stderr(  # pragma: no cover
                self,
                *,
                process: object,
                log_path: Path,
                stderr_tail: deque[str],
            ) -> None:
                return None

        old_stream = vibes.TelegramStream
        old_panel = vibes.PanelUI
        old_state_path = vibes.STATE_PATH
        old_log_dir = vibes.LOG_DIR
        old_bot_log_path = vibes.BOT_LOG_PATH

        try:
            with TemporaryDirectory() as td:
                tmp = Path(td)
                vibes.STATE_PATH = tmp / "state.json"
                vibes.LOG_DIR = tmp / "logs"
                vibes.BOT_LOG_PATH = tmp / "bot.log"
                vibes.TelegramStream = _CapturingStream  # type: ignore[assignment]
                vibes.PanelUI = _CapturingPanelUI  # type: ignore[assignment]

                manager = _RunPromptManager(admin_id=None)
                manager.sessions = {"S": vibes.SessionRecord(name="S", path=".")}

                await manager.run_prompt(
                    chat_id=1,
                    panel_message_id=123,
                    application=object(),  # ignored by stubs
                    session_name="S",
                    prompt="hello",
                    run_mode="new",
                )
        finally:
            vibes.TelegramStream = old_stream
            vibes.PanelUI = old_panel
            vibes.STATE_PATH = old_state_path
            vibes.LOG_DIR = old_log_dir
            vibes.BOT_LOG_PATH = old_bot_log_path

        panel = _CapturingPanelUI.last_instance
        self.assertIsNotNone(panel)
        self.assertTrue(panel.renders)

        _chat_id, _message_id, text_html, reply_markup = panel.renders[-1]
        self.assertIn("Send a prompt to continue.", text_html)

        buttons = getattr(reply_markup, "inline_keyboard", [])
        texts = [getattr(btn, "text", "") for row in buttons for btn in (row or [])]
        self.assertIn("🆕", texts)
        self.assertIn("⚙️", texts)
        self.assertIn(vibes.LABEL_BACK, texts)
        self.assertIn("🗑", texts)

    async def test_run_prompt_uses_claude_cwd_and_resume(self) -> None:
        class _CapturingPanelUI:
            def __init__(self, application: object, manager: object) -> None:  # pragma: no cover
                return None

            async def render_to_message(
                self,
                *,
                chat_id: int,
                message_id: int,
                text_html: str,
                reply_markup: object,
                update_state_on_replace: bool,
            ) -> int:  # pragma: no cover
                return message_id

        class _CapturingStream:
            def __init__(
                self,
                application: object,
                chat_id: int,
                message_id: int,
                *,
                header_html: str = "",
                header_plain_len: int = 0,
                auto_clear_header_on_first_log: bool = False,
                footer_provider: object | None = None,
                footer_plain_len: int = 0,
                wrap_log_in_pre: bool = False,
                reply_markup: object | None = None,
                on_panel_replaced: object | None = None,
            ) -> None:
                self._chat_id = chat_id
                self._message_id = message_id

            def get_chat_id(self) -> int:  # pragma: no cover
                return self._chat_id

            def get_message_id(self) -> int:  # pragma: no cover
                return self._message_id

            async def pause(self) -> None:  # pragma: no cover
                return None

            async def resume(self) -> None:  # pragma: no cover
                return None

            async def stop(self) -> None:  # pragma: no cover
                return None

        class _FakeProcess:
            def __init__(self, return_code: int) -> None:
                self.returncode: int | None = None
                self._return_code = return_code
                self.pid = 123

            async def wait(self) -> int:
                self.returncode = self._return_code
                return self._return_code

        class _RunPromptManager(vibes.SessionManager):
            def __init__(self, admin_id: int | None) -> None:
                super().__init__(admin_id=admin_id)
                self.seen_cmd: list[str] | None = None
                self.seen_cwd: str | None = None

            async def save_state(self) -> None:  # pragma: no cover
                return None

            async def _spawn_process(self, cmd: list[str], *, cwd: str | None = None) -> object:
                self.seen_cmd = list(cmd)
                self.seen_cwd = cwd
                return _FakeProcess(return_code=0)

            async def _read_stdout(  # pragma: no cover
                self,
                *,
                rec: vibes.SessionRecord,
                process: object,
                stream: object,
                log_path: Path,
            ) -> None:
                return None

            async def _read_stderr(  # pragma: no cover
                self,
                *,
                process: object,
                log_path: Path,
                stderr_tail: deque[str],
            ) -> None:
                return None

        class _CapturingBot:
            async def send_message(self, **kwargs: object) -> None:  # pragma: no cover
                return None

        class _App:
            def __init__(self) -> None:
                self.bot = _CapturingBot()

        old_stream = vibes.TelegramStream
        old_panel = vibes.PanelUI
        old_state_path = vibes.STATE_PATH
        old_log_dir = vibes.LOG_DIR
        old_bot_log_path = vibes.BOT_LOG_PATH

        app = _App()

        try:
            with TemporaryDirectory() as td:
                tmp = Path(td)
                vibes.STATE_PATH = tmp / "state.json"
                vibes.LOG_DIR = tmp / "logs"
                vibes.BOT_LOG_PATH = tmp / "bot.log"
                vibes.TelegramStream = _CapturingStream  # type: ignore[assignment]
                vibes.PanelUI = _CapturingPanelUI  # type: ignore[assignment]

                manager = _RunPromptManager(admin_id=None)
                rec = vibes.SessionRecord(
                    name="S",
                    path=str(tmp / "proj"),
                    engine=vibes.ENGINE_CLAUDE,
                    thread_id="03a97da8-27b5-4b56-aa1f-b3231ef42f10",
                    model="sonnet",
                )
                manager.sessions = {"S": rec}

                await manager.run_prompt(
                    chat_id=1,
                    panel_message_id=123,
                    application=app,
                    session_name="S",
                    prompt="hello",
                    run_mode="continue",
                )

                self.assertEqual(manager.seen_cwd, rec.path)
                self.assertIsNotNone(manager.seen_cmd)
                cmd = manager.seen_cmd or []
                self.assertIn("claude", cmd[0])
                self.assertIn("--output-format", cmd)
                self.assertIn("stream-json", cmd)
                self.assertIn("-r", cmd)
        finally:
            vibes.TelegramStream = old_stream
            vibes.PanelUI = old_panel
            vibes.STATE_PATH = old_state_path
            vibes.LOG_DIR = old_log_dir
            vibes.BOT_LOG_PATH = old_bot_log_path

    async def test_run_prompt_does_not_send_completion_notice(self) -> None:
        class _CapturingPanelUI:
            def __init__(self, application: object, manager: object) -> None:  # pragma: no cover
                return None

            async def render_to_message(
                self,
                *,
                chat_id: int,
                message_id: int,
                text_html: str,
                reply_markup: object,
                update_state_on_replace: bool,
            ) -> int:  # pragma: no cover
                return message_id

        class _CapturingStream:
            def __init__(
                self,
                application: object,
                chat_id: int,
                message_id: int,
                *,
                header_html: str = "",
                header_plain_len: int = 0,
                auto_clear_header_on_first_log: bool = False,
                footer_provider: object | None = None,
                footer_plain_len: int = 0,
                wrap_log_in_pre: bool = False,
                reply_markup: object | None = None,
                on_panel_replaced: object | None = None,
            ) -> None:
                self._chat_id = chat_id
                self._message_id = message_id

            def get_chat_id(self) -> int:  # pragma: no cover
                return self._chat_id

            def get_message_id(self) -> int:  # pragma: no cover
                return self._message_id

            async def stop(self) -> None:  # pragma: no cover
                return None

        class _FakeProcess:
            def __init__(self, return_code: int) -> None:
                self.returncode: int | None = None
                self._return_code = return_code

            async def wait(self) -> int:
                self.returncode = self._return_code
                return self._return_code

        class _RunPromptManager(vibes.SessionManager):
            async def save_state(self) -> None:  # pragma: no cover
                return None

            async def _spawn_process(self, cmd: list[str], *, cwd: str | None = None) -> object:
                return _FakeProcess(return_code=0)

            async def _read_stdout(  # pragma: no cover
                self,
                *,
                rec: vibes.SessionRecord,
                process: object,
                stream: object,
                log_path: Path,
            ) -> None:
                return None

            async def _read_stderr(  # pragma: no cover
                self,
                *,
                process: object,
                log_path: Path,
                stderr_tail: deque[str],
            ) -> None:
                return None

        class _CapturingBot:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send_message(self, **kwargs: object) -> None:
                self.sent.append(dict(kwargs))

        class _App:
            def __init__(self) -> None:
                self.bot = _CapturingBot()

        old_stream = vibes.TelegramStream
        old_panel = vibes.PanelUI
        old_state_path = vibes.STATE_PATH
        old_log_dir = vibes.LOG_DIR
        old_bot_log_path = vibes.BOT_LOG_PATH

        app = _App()

        try:
            with TemporaryDirectory() as td:
                tmp = Path(td)
                vibes.STATE_PATH = tmp / "state.json"
                vibes.LOG_DIR = tmp / "logs"
                vibes.BOT_LOG_PATH = tmp / "bot.log"
                vibes.TelegramStream = _CapturingStream  # type: ignore[assignment]
                vibes.PanelUI = _CapturingPanelUI  # type: ignore[assignment]

                manager = _RunPromptManager(admin_id=None)
                manager.sessions = {"S": vibes.SessionRecord(name="S", path=".")}

                await manager.run_prompt(
                    chat_id=1,
                    panel_message_id=123,
                    application=app,  # has bot.send_message
                    session_name="S",
                    prompt="hello",
                    run_mode="new",
                )
        finally:
            vibes.TelegramStream = old_stream
            vibes.PanelUI = old_panel
            vibes.STATE_PATH = old_state_path
            vibes.LOG_DIR = old_log_dir
            vibes.BOT_LOG_PATH = old_bot_log_path

        self.assertEqual(len(app.bot.sent), 1)

    async def test_run_prompt_does_not_send_completion_notice_on_retry_after(self) -> None:
        class _CapturingPanelUI:
            def __init__(self, application: object, manager: object) -> None:  # pragma: no cover
                return None

            async def render_to_message(
                self,
                *,
                chat_id: int,
                message_id: int,
                text_html: str,
                reply_markup: object,
                update_state_on_replace: bool,
            ) -> int:  # pragma: no cover
                return message_id

        class _CapturingStream:
            def __init__(
                self,
                application: object,
                chat_id: int,
                message_id: int,
                *,
                header_html: str = "",
                header_plain_len: int = 0,
                auto_clear_header_on_first_log: bool = False,
                footer_provider: object | None = None,
                footer_plain_len: int = 0,
                wrap_log_in_pre: bool = False,
                reply_markup: object | None = None,
                on_panel_replaced: object | None = None,
            ) -> None:
                self._chat_id = chat_id
                self._message_id = message_id

            def get_chat_id(self) -> int:  # pragma: no cover
                return self._chat_id

            def get_message_id(self) -> int:  # pragma: no cover
                return self._message_id

            async def stop(self) -> None:  # pragma: no cover
                return None

        class _FakeProcess:
            def __init__(self, return_code: int) -> None:
                self.returncode: int | None = None
                self._return_code = return_code

            async def wait(self) -> int:
                self.returncode = self._return_code
                return self._return_code

        class _RunPromptManager(vibes.SessionManager):
            async def save_state(self) -> None:  # pragma: no cover
                return None

            async def _spawn_process(self, cmd: list[str], *, cwd: str | None = None) -> object:
                return _FakeProcess(return_code=0)

            async def _read_stdout(  # pragma: no cover
                self,
                *,
                rec: vibes.SessionRecord,
                process: object,
                stream: object,
                log_path: Path,
            ) -> None:
                return None

            async def _read_stderr(  # pragma: no cover
                self,
                *,
                process: object,
                log_path: Path,
                stderr_tail: deque[str],
            ) -> None:
                return None

        class _FlakyBot:
            def __init__(self) -> None:
                self.calls = 0
                self.sent: list[dict] = []

            async def send_message(self, **kwargs: object) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise vibes.RetryAfter(0.0)
                self.sent.append(dict(kwargs))

        class _App:
            def __init__(self) -> None:
                self.bot = _FlakyBot()

        old_stream = vibes.TelegramStream
        old_panel = vibes.PanelUI
        old_state_path = vibes.STATE_PATH
        old_log_dir = vibes.LOG_DIR
        old_bot_log_path = vibes.BOT_LOG_PATH

        app = _App()

        try:
            with TemporaryDirectory() as td:
                tmp = Path(td)
                vibes.STATE_PATH = tmp / "state.json"
                vibes.LOG_DIR = tmp / "logs"
                vibes.BOT_LOG_PATH = tmp / "bot.log"
                vibes.TelegramStream = _CapturingStream  # type: ignore[assignment]
                vibes.PanelUI = _CapturingPanelUI  # type: ignore[assignment]

                manager = _RunPromptManager(admin_id=None)
                manager.sessions = {"S": vibes.SessionRecord(name="S", path=".")}

                await manager.run_prompt(
                    chat_id=1,
                    panel_message_id=123,
                    application=app,  # has bot.send_message
                    session_name="S",
                    prompt="hello",
                    run_mode="new",
                )
        finally:
            vibes.TelegramStream = old_stream
            vibes.PanelUI = old_panel
            vibes.STATE_PATH = old_state_path
            vibes.LOG_DIR = old_log_dir
            vibes.BOT_LOG_PATH = old_bot_log_path

        self.assertEqual(app.bot.calls, 1)
        self.assertEqual(len(app.bot.sent), 0)
