import os
import unittest
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
    def __init__(self, *, text: str, message_id: int = 1) -> None:
        self.text = text
        self.message_id = message_id
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class _FakeCallbackMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class _FakeCallbackQuery:
    def __init__(self, *, data: str, message_id: int) -> None:
        self.data = data
        self.message = _FakeCallbackMessage(message_id)
        self.answered = False

    async def answer(self) -> None:
        self.answered = True


class _FakeTextUpdate:
    def __init__(self, *, chat_id: int, user_id: int, text: str) -> None:
        self.effective_chat = _FakeChat(chat_id)
        self.effective_user = _FakeUser(user_id)
        self.message = _FakeMessage(text=text)


class _FakeCallbackUpdate:
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


class _FakeApplication:
    def __init__(self, *, manager: vibes.SessionManager, panel: object) -> None:
        self.bot_data = {"manager": manager, "panel": panel}


class _FakeContext:
    def __init__(self, *, application: _FakeApplication, chat_data: dict) -> None:
        self.application = application
        self.chat_data = chat_data


class MkdirFlowTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_new_path_missing_dir_offers_create_and_creates_on_confirm(self) -> None:
        with TemporaryDirectory() as td:
            base = Path(td)
            missing = base / "a" / "b"
            self.assertFalse(missing.exists())

            chat_id = 1
            panel_message_id = 10

            manager = vibes.SessionManager(admin_id=1)
            manager.panel_by_chat = {chat_id: panel_message_id}
            panel = _FakePanelUI(fixed_panel_message_id=panel_message_id)
            ctx = _FakeContext(
                application=_FakeApplication(manager=manager, panel=panel),
                chat_data={"ui": {"mode": "new_path", "new": {"name": "S", "path_mode": "full"}}},
            )

            await vibes.on_text(  # type: ignore[arg-type]
                _FakeTextUpdate(chat_id=chat_id, user_id=1, text=str(missing)),
                ctx,
            )

            ui = ctx.chat_data.get("ui", {})
            self.assertEqual(ui.get("mode"), "confirm_mkdir")
            self.assertEqual(ui.get("mkdir", {}).get("flow"), "new_path")

            await vibes.on_callback(  # type: ignore[arg-type]
                _FakeCallbackUpdate(
                    chat_id=chat_id,
                    user_id=1,
                    query=_FakeCallbackQuery(data=vibes._cb("mkdir_yes"), message_id=panel_message_id),
                ),
                ctx,
            )

            self.assertTrue(missing.exists())
            self.assertTrue(missing.is_dir())
            self.assertIn("S", manager.sessions)
            self.assertEqual(ctx.chat_data.get("ui", {}).get("mode"), "session")

    async def test_new_path_simple_name_uses_default_root(self) -> None:
        with TemporaryDirectory() as td:
            base = Path(td) / "Documents"
            chat_id = 1
            panel_message_id = 10

            manager = vibes.SessionManager(admin_id=1)
            manager.panel_by_chat = {chat_id: panel_message_id}
            panel = _FakePanelUI(fixed_panel_message_id=panel_message_id)
            ctx = _FakeContext(
                application=_FakeApplication(manager=manager, panel=panel),
                chat_data={"ui": {"mode": "new_path", "new": {"name": "S", "path_mode": "docs"}}},
            )

            old_env = os.environ.get("VIBES_DEFAULT_PROJECTS_DIR")
            os.environ["VIBES_DEFAULT_PROJECTS_DIR"] = str(base)
            try:
                await vibes.on_text(  # type: ignore[arg-type]
                    _FakeTextUpdate(chat_id=chat_id, user_id=1, text="demo"),
                    ctx,
                )
            finally:
                if old_env is None:
                    os.environ.pop("VIBES_DEFAULT_PROJECTS_DIR", None)
                else:
                    os.environ["VIBES_DEFAULT_PROJECTS_DIR"] = old_env

            ui = ctx.chat_data.get("ui", {})
            expected = (base / "demo").resolve()
            self.assertEqual(ui.get("mode"), "confirm_mkdir")
            self.assertEqual(ui.get("mkdir", {}).get("path"), str(expected))

    async def test_paths_add_missing_dir_offers_create_and_adds_preset(self) -> None:
        with TemporaryDirectory() as td:
            base = Path(td)
            missing = base / "preset"
            self.assertFalse(missing.exists())

            chat_id = 1
            panel_message_id = 10

            manager = vibes.SessionManager(admin_id=1)
            manager.panel_by_chat = {chat_id: panel_message_id}
            panel = _FakePanelUI(fixed_panel_message_id=panel_message_id)
            ctx = _FakeContext(
                application=_FakeApplication(manager=manager, panel=panel),
                chat_data={"ui": {"mode": "paths_add"}},
            )

            await vibes.on_text(  # type: ignore[arg-type]
                _FakeTextUpdate(chat_id=chat_id, user_id=1, text=str(missing)),
                ctx,
            )

            ui = ctx.chat_data.get("ui", {})
            self.assertEqual(ui.get("mode"), "confirm_mkdir")
            self.assertEqual(ui.get("mkdir", {}).get("flow"), "paths_add")

            await vibes.on_callback(  # type: ignore[arg-type]
                _FakeCallbackUpdate(
                    chat_id=chat_id,
                    user_id=1,
                    query=_FakeCallbackQuery(data=vibes._cb("mkdir_yes"), message_id=panel_message_id),
                ),
                ctx,
            )

            self.assertTrue(missing.exists())
            self.assertTrue(missing.is_dir())
            self.assertIn(str(missing.resolve()), manager.path_presets)
            self.assertEqual(ctx.chat_data.get("ui", {}).get("mode"), "paths")

    async def test_ack_callback_deletes_message(self) -> None:
        chat_id = 1
        panel_message_id = 10
        ack_message_id = 123

        manager = vibes.SessionManager(admin_id=1)
        manager.panel_by_chat = {chat_id: panel_message_id}

        panel = _FakePanelUI(fixed_panel_message_id=panel_message_id)
        ctx = _FakeContext(application=_FakeApplication(manager=manager, panel=panel), chat_data={})

        await vibes.on_callback(  # type: ignore[arg-type]
            _FakeCallbackUpdate(
                chat_id=chat_id,
                user_id=1,
                query=_FakeCallbackQuery(data=vibes._cb("ack"), message_id=ack_message_id),
            ),
            ctx,
        )

        self.assertIn((chat_id, ack_message_id), panel.deletes)
