import asyncio
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory

import telegram_stubs

telegram_stubs.install()

import vibes  # noqa: E402


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class _FakeCallbackQuery:
    def __init__(self, *, data: str, message_id: int) -> None:
        self.data = data
        self.message = _FakeMessage(message_id)
        self.answered = False

    async def answer(self) -> None:
        self.answered = True


class _FakeUpdate:
    def __init__(self, *, chat_id: int, user_id: int, query: _FakeCallbackQuery) -> None:
        self.effective_chat = _FakeChat(chat_id)
        self.effective_user = _FakeUser(user_id)
        self.callback_query = query


class _FakePanelUI:
    def __init__(self, fixed_panel_message_id: int) -> None:
        self.fixed_panel_message_id = fixed_panel_message_id
        self.renders: list[tuple[int, int, str, object]] = []
        self.deletes: list[tuple[int, int]] = []

    async def ensure_panel(self, _chat_id: int) -> int:
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
        self.renders.append((chat_id, message_id, text_html, reply_markup))
        return message_id

    async def delete_message_best_effort(self, *, chat_id: int, message_id: int) -> None:
        self.deletes.append((chat_id, message_id))


class _NoopManager(vibes.SessionManager):
    def __init__(self, *, admin_id: int | None) -> None:
        super().__init__(admin_id=admin_id)
        self.stop_calls: list[str] = []

    async def save_state(self) -> None:  # pragma: no cover
        return None

    async def stop(self, name: str, *, reason: str = "user") -> bool:  # pragma: no cover
        self.stop_calls.append(name)
        rec = self.sessions.get(name)
        if rec and rec.run:
            rec.run.stop_requested = True
        return True


class _FakeApplication:
    def __init__(self, *, manager: vibes.SessionManager, panel: object) -> None:
        self.bot_data = {"manager": manager, "panel": panel}


class _FakeContext:
    def __init__(self, *, application: _FakeApplication, chat_data: dict) -> None:
        self.application = application
        self.chat_data = chat_data


class _FakeProcess:
    returncode = None


class _CapturingRunStream:
    def __init__(self, *, chat_id: int, message_id: int) -> None:
        self._chat_id = chat_id
        self._message_id = message_id
        self.headers: list[str] = []
        self.markups: list[object | None] = []
        self.paused = False

    def get_chat_id(self) -> int:
        return self._chat_id

    def get_message_id(self) -> int:
        return self._message_id

    async def set_header(self, *, header_html: str, header_plain_len: int | None = None) -> None:
        self.headers.append(header_html)

    async def set_reply_markup(self, reply_markup: object | None) -> None:
        self.markups.append(reply_markup)

    async def pause(self) -> None:
        self.paused = True

    async def resume(self) -> None:
        self.paused = False


class CallbackFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp_dir = TemporaryDirectory()
        tmp = Path(self._tmp_dir.name)
        self._old_state_path = vibes.STATE_PATH
        self._old_log_dir = vibes.LOG_DIR
        self._old_bot_log_path = vibes.BOT_LOG_PATH
        vibes.STATE_PATH = tmp / "state.json"
        vibes.LOG_DIR = tmp / "logs"
        vibes.BOT_LOG_PATH = tmp / "bot.log"

    async def asyncTearDown(self) -> None:
        vibes.STATE_PATH = self._old_state_path
        vibes.LOG_DIR = self._old_log_dir
        vibes.BOT_LOG_PATH = self._old_bot_log_path
        self._tmp_dir.cleanup()

    async def test_new_session_wizard_via_callbacks(self) -> None:
        with TemporaryDirectory() as td:
            work = Path(td)
            chat_id = 1
            panel_message_id = 10

            manager = _NoopManager(admin_id=1)
            manager.panel_by_chat = {chat_id: panel_message_id}
            manager.path_presets = [str(work)]

            panel = _FakePanelUI(fixed_panel_message_id=panel_message_id)
            ctx = _FakeContext(application=_FakeApplication(manager=manager, panel=panel), chat_data={})

            # 1) Click "new"
            update_new = _FakeUpdate(
                chat_id=chat_id,
                user_id=1,
                query=_FakeCallbackQuery(data=vibes._cb("new"), message_id=panel_message_id),
            )
            await vibes.on_callback(update_new, ctx)  # type: ignore[arg-type]
            self.assertEqual(ctx.chat_data.get("ui", {}).get("mode"), "new_name")
            self.assertTrue(panel.renders)
            self.assertIn("Step 1/3", panel.renders[-1][2])

            # 2) Pick auto name
            update_auto = _FakeUpdate(
                chat_id=chat_id,
                user_id=1,
                query=_FakeCallbackQuery(data=vibes._cb("new_auto"), message_id=panel_message_id),
            )
            await vibes.on_callback(update_auto, ctx)  # type: ignore[arg-type]
            self.assertEqual(ctx.chat_data.get("ui", {}).get("mode"), "new_engine")
            draft = ctx.chat_data["ui"]["new"]
            self.assertIn("name", draft)

            # 3) Pick engine
            update_engine = _FakeUpdate(
                chat_id=chat_id,
                user_id=1,
                query=_FakeCallbackQuery(data=vibes._cb("engine", "codex"), message_id=panel_message_id),
            )
            await vibes.on_callback(update_engine, ctx)  # type: ignore[arg-type]
            self.assertEqual(ctx.chat_data.get("ui", {}).get("mode"), "new_path")
            draft = ctx.chat_data["ui"]["new"]
            self.assertEqual(draft.get("engine"), "codex")

            # 4) Pick path mode
            update_mode = _FakeUpdate(
                chat_id=chat_id,
                user_id=1,
                query=_FakeCallbackQuery(data=vibes._cb("path_mode", "full"), message_id=panel_message_id),
            )
            await vibes.on_callback(update_mode, ctx)  # type: ignore[arg-type]
            self.assertEqual(ctx.chat_data.get("ui", {}).get("mode"), "new_path")
            draft = ctx.chat_data["ui"]["new"]
            self.assertEqual(draft.get("path_mode"), "full")

            # 5) Pick path preset -> session created
            update_pick = _FakeUpdate(
                chat_id=chat_id,
                user_id=1,
                query=_FakeCallbackQuery(data=vibes._cb("path_pick", "0"), message_id=panel_message_id),
            )
            await vibes.on_callback(update_pick, ctx)  # type: ignore[arg-type]
            self.assertEqual(ctx.chat_data.get("ui", {}).get("mode"), "session")
            session_name = ctx.chat_data.get("ui", {}).get("session")
            self.assertIn(session_name, manager.sessions)

    async def test_interrupt_requests_stop_on_attached_run(self) -> None:
        chat_id = 1
        panel_message_id = 10

        manager = _NoopManager(admin_id=1)
        manager.panel_by_chat = {chat_id: panel_message_id}
        panel = _FakePanelUI(fixed_panel_message_id=panel_message_id)
        ctx = _FakeContext(application=_FakeApplication(manager=manager, panel=panel), chat_data={"ui": {"mode": "session", "session": "S"}})

        rec = vibes.SessionRecord(name="S", path=".")
        rec.status = "running"
        stream = _CapturingRunStream(chat_id=chat_id, message_id=panel_message_id)
        rec.run = vibes.SessionRun(
            process=_FakeProcess(),  # type: ignore[arg-type]
            stdout_task=asyncio.create_task(asyncio.sleep(0)),
            stderr_task=asyncio.create_task(asyncio.sleep(0)),
            stream=stream,  # type: ignore[arg-type]
            stdout_log=Path("stdout.jsonl"),
            stderr_log=Path("stderr.txt"),
            stderr_tail=deque(),
        )
        manager.sessions = {"S": rec}

        update_interrupt = _FakeUpdate(
            chat_id=chat_id,
            user_id=1,
            query=_FakeCallbackQuery(data=vibes._cb("interrupt"), message_id=panel_message_id),
        )
        await vibes.on_callback(update_interrupt, ctx)  # type: ignore[arg-type]

        self.assertIn("S", manager.stop_calls)
        self.assertTrue(rec.run.stop_requested)
        self.assertFalse(stream.paused)
        self.assertFalse(panel.renders)

    async def test_restart_sets_restart_event_when_idle(self) -> None:
        chat_id = 1
        panel_message_id = 10

        manager = _NoopManager(admin_id=1)
        manager.panel_by_chat = {chat_id: panel_message_id}
        panel = _FakePanelUI(fixed_panel_message_id=panel_message_id)

        app = _FakeApplication(manager=manager, panel=panel)
        app.bot_data["restart_event"] = asyncio.Event()
        ctx = _FakeContext(application=app, chat_data={})

        update_restart = _FakeUpdate(
            chat_id=chat_id,
            user_id=1,
            query=_FakeCallbackQuery(data=vibes._cb("restart"), message_id=panel_message_id),
        )
        await vibes.on_callback(update_restart, ctx)  # type: ignore[arg-type]

        await asyncio.sleep(0.3)
        self.assertTrue(app.bot_data["restart_event"].is_set())

    async def test_back_from_session_after_session_reopen_is_one_click(self) -> None:
        """
        Regression: clicking a "⬅️ (session)" button while already on the same session
        used to push a duplicate nav entry, so the session screen required 2+ back clicks.
        """
        chat_id = 1
        panel_message_id = 10

        manager = _NoopManager(admin_id=1)
        manager.panel_by_chat = {chat_id: panel_message_id}
        manager.sessions = {"S": vibes.SessionRecord(name="S", path=".")}

        panel = _FakePanelUI(fixed_panel_message_id=panel_message_id)
        ctx = _FakeContext(
            application=_FakeApplication(manager=manager, panel=panel),
            chat_data={"ui": {"mode": "session", "session": "S", "nav": [{"mode": "sessions"}]}},
        )

        # Simulate clicking the post-run "back to session" button while already on that session.
        update_session = _FakeUpdate(
            chat_id=chat_id,
            user_id=1,
            query=_FakeCallbackQuery(data=vibes._cb("session", "S"), message_id=panel_message_id),
        )
        await vibes.on_callback(update_session, ctx)  # type: ignore[arg-type]

        update_back = _FakeUpdate(
            chat_id=chat_id,
            user_id=1,
            query=_FakeCallbackQuery(data=vibes._cb("back"), message_id=panel_message_id),
        )
        await vibes.on_callback(update_back, ctx)  # type: ignore[arg-type]

        self.assertEqual(ctx.chat_data.get("ui", {}).get("mode"), "sessions")

    async def test_back_skips_duplicate_snapshots(self) -> None:
        chat_id = 1
        panel_message_id = 10

        manager = _NoopManager(admin_id=1)
        manager.panel_by_chat = {chat_id: panel_message_id}
        manager.sessions = {"S": vibes.SessionRecord(name="S", path=".")}

        panel = _FakePanelUI(fixed_panel_message_id=panel_message_id)
        # Two identical session snapshots on top of the stack should be skipped in one back click.
        ctx = _FakeContext(
            application=_FakeApplication(manager=manager, panel=panel),
            chat_data={
                "ui": {
                    "mode": "session",
                    "session": "S",
                    "nav": [
                        {"mode": "sessions"},
                        {"mode": "session", "session": "S"},
                        {"mode": "session", "session": "S"},
                    ],
                }
            },
        )

        update_back = _FakeUpdate(
            chat_id=chat_id,
            user_id=1,
            query=_FakeCallbackQuery(data=vibes._cb("back"), message_id=panel_message_id),
        )
        await vibes.on_callback(update_back, ctx)  # type: ignore[arg-type]
        self.assertEqual(ctx.chat_data.get("ui", {}).get("mode"), "sessions")

    async def test_session_switch_pushes_nav_and_back_returns_to_previous_session(self) -> None:
        chat_id = 1
        panel_message_id = 10

        manager = _NoopManager(admin_id=1)
        manager.panel_by_chat = {chat_id: panel_message_id}
        manager.sessions = {
            "A": vibes.SessionRecord(name="A", path="."),
            "B": vibes.SessionRecord(name="B", path="."),
        }

        panel = _FakePanelUI(fixed_panel_message_id=panel_message_id)
        ctx = _FakeContext(
            application=_FakeApplication(manager=manager, panel=panel),
            chat_data={"ui": {"mode": "session", "session": "A", "nav": [{"mode": "sessions"}]}},
        )

        update_open_b = _FakeUpdate(
            chat_id=chat_id,
            user_id=1,
            query=_FakeCallbackQuery(data=vibes._cb("session", "B"), message_id=panel_message_id),
        )
        await vibes.on_callback(update_open_b, ctx)  # type: ignore[arg-type]
        self.assertEqual(ctx.chat_data.get("ui", {}).get("mode"), "session")
        self.assertEqual(ctx.chat_data.get("ui", {}).get("session"), "B")

        update_back = _FakeUpdate(
            chat_id=chat_id,
            user_id=1,
            query=_FakeCallbackQuery(data=vibes._cb("back"), message_id=panel_message_id),
        )
        await vibes.on_callback(update_back, ctx)  # type: ignore[arg-type]

        self.assertEqual(ctx.chat_data.get("ui", {}).get("mode"), "session")
        self.assertEqual(ctx.chat_data.get("ui", {}).get("session"), "A")

    async def test_model_and_reasoning_pick_stays_on_settings_screen_and_highlights_selection(self) -> None:
        chat_id = 1
        panel_message_id = 10

        manager = _NoopManager(admin_id=1)
        manager.panel_by_chat = {chat_id: panel_message_id}
        manager.sessions = {"S": vibes.SessionRecord(name="S", path=".")}

        panel = _FakePanelUI(fixed_panel_message_id=panel_message_id)
        ctx = _FakeContext(
            application=_FakeApplication(manager=manager, panel=panel),
            chat_data={"ui": {"mode": "model", "session": "S", "nav": [{"mode": "session", "session": "S"}]}},
        )

        update_model = _FakeUpdate(
            chat_id=chat_id,
            user_id=1,
            query=_FakeCallbackQuery(data=vibes._cb("model_pick", "0"), message_id=panel_message_id),
        )
        await vibes.on_callback(update_model, ctx)  # type: ignore[arg-type]

        self.assertEqual(ctx.chat_data.get("ui", {}).get("mode"), "model")
        self.assertEqual(manager.sessions["S"].model, vibes.MODEL_PRESETS[0])

        markup = panel.renders[-1][3]
        model_btn = None
        for row in markup.inline_keyboard:
            for btn in row:
                if getattr(btn, "callback_data", None) == vibes._cb("model_pick", "0"):
                    model_btn = btn
                    break
        self.assertIsNotNone(model_btn)
        self.assertTrue(getattr(model_btn, "text", "").startswith("✅ "))

        update_reasoning = _FakeUpdate(
            chat_id=chat_id,
            user_id=1,
            query=_FakeCallbackQuery(data=vibes._cb("reasoning_pick", "medium"), message_id=panel_message_id),
        )
        await vibes.on_callback(update_reasoning, ctx)  # type: ignore[arg-type]

        self.assertEqual(ctx.chat_data.get("ui", {}).get("mode"), "model")
        self.assertEqual(manager.sessions["S"].reasoning_effort, "medium")

        markup2 = panel.renders[-1][3]
        reasoning_btn = None
        for row in markup2.inline_keyboard:
            for btn in row:
                if getattr(btn, "callback_data", None) == vibes._cb("reasoning_pick", "medium"):
                    reasoning_btn = btn
                    break
        self.assertIsNotNone(reasoning_btn)
        self.assertTrue(getattr(reasoning_btn, "text", "").startswith("✅ "))
