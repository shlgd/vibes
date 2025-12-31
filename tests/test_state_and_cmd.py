import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import telegram_stubs

telegram_stubs.install()

import vibes  # noqa: E402


class SessionManagerStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp_dir = TemporaryDirectory()
        self.tmp = Path(self._tmp_dir.name)

        self._old_state_path = vibes.STATE_PATH
        self._old_log_dir = vibes.LOG_DIR
        self._old_bot_log_path = vibes.BOT_LOG_PATH

        vibes.STATE_PATH = self.tmp / "state.json"
        vibes.LOG_DIR = self.tmp / "logs"
        vibes.BOT_LOG_PATH = self.tmp / "bot.log"

    async def asyncTearDown(self) -> None:
        vibes.STATE_PATH = self._old_state_path
        vibes.LOG_DIR = self._old_log_dir
        vibes.BOT_LOG_PATH = self._old_bot_log_path
        self._tmp_dir.cleanup()

    async def test_state_roundtrip_sessions_and_presets(self) -> None:
        work = self.tmp / "work"
        work.mkdir()

        manager = vibes.SessionManager(admin_id=1)
        rec, err = await manager.create_session(name="S1", path=str(work))
        self.assertIsNotNone(rec)
        self.assertEqual(err, "")

        rec.model = "gpt-5.2-codex"
        rec.reasoning_effort = "xhigh"
        await manager.upsert_path_preset(str(work))
        await manager.save_state()

        manager2 = vibes.SessionManager(admin_id=1)
        self.assertIn("S1", manager2.sessions)
        self.assertEqual(manager2.sessions["S1"].path, str(work.resolve()))
        self.assertEqual(manager2.sessions["S1"].model, "gpt-5.2-codex")
        self.assertEqual(manager2.sessions["S1"].reasoning_effort, "xhigh")
        self.assertEqual([Path(p).resolve() for p in manager2.path_presets], [work.resolve()])

    async def test_build_codex_cmd_flags_and_resume(self) -> None:
        work = self.tmp / "no_git"
        work.mkdir()

        manager = vibes.SessionManager(admin_id=1)
        rec = vibes.SessionRecord(name="S", path=str(work))
        rec.reasoning_effort = "low"
        rec.thread_id = "thread-123"

        cmd = manager._build_codex_cmd(rec, prompt="hello", run_mode="continue")
        joined = " ".join(cmd)
        self.assertIn("codex exec", joined)
        self.assertIn("--json", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertIn("workspace-write", cmd)
        self.assertNotIn("--add-dir", cmd)
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("gpt-5.2", cmd)
        self.assertIn("model_reasoning_effort=low", joined)
        self.assertIn("resume", cmd)
        self.assertIn("thread-123", cmd)

    async def test_build_codex_cmd_skips_skip_git_when_git_present(self) -> None:
        work = self.tmp / "has_git"
        (work / ".git").mkdir(parents=True)

        manager = vibes.SessionManager(admin_id=1)
        rec = vibes.SessionRecord(name="S", path=str(work))

        cmd = manager._build_codex_cmd(rec, prompt="hello", run_mode="new")
        self.assertIn("--add-dir", cmd)
        add_dir_idx = cmd.index("--add-dir") + 1
        self.assertEqual(cmd[add_dir_idx], str((work / ".git").resolve()))
        self.assertNotIn("--skip-git-repo-check", cmd)

    async def test_build_codex_cmd_inserts_end_of_options_for_dash_prompt(self) -> None:
        work = self.tmp / "no_git_dash"
        work.mkdir()

        manager = vibes.SessionManager(admin_id=1)
        rec = vibes.SessionRecord(name="S", path=str(work))
        rec.thread_id = "thread-123"

        cmd_new = manager._build_codex_cmd(rec, prompt="--help", run_mode="new")
        self.assertEqual(cmd_new[-2:], ["--", "--help"])

        cmd_resume = manager._build_codex_cmd(rec, prompt="--help", run_mode="continue")
        self.assertEqual(cmd_resume[-4:], ["resume", "thread-123", "--", "--help"])

    async def test_create_session_validates_name_and_path(self) -> None:
        manager = vibes.SessionManager(admin_id=1)

        rec, err = await manager.create_session(name="bad name", path=str(self.tmp))
        self.assertIsNone(rec)
        self.assertIn("Invalid name", err)

        rec2, err2 = await manager.create_session(name="ok", path=str(self.tmp / "missing"))
        self.assertIsNone(rec2)
        self.assertIn("Directory not found", err2)


class SessionArtifactsTests(unittest.TestCase):
    def test_delete_session_artifacts_removes_known_files(self) -> None:
        with TemporaryDirectory() as td:
            tmp = Path(td)
            old_state_path = vibes.STATE_PATH
            old_log_dir = vibes.LOG_DIR
            old_bot_log_path = vibes.BOT_LOG_PATH
            try:
                vibes.STATE_PATH = tmp / "state.json"
                vibes.LOG_DIR = tmp / "logs"
                vibes.BOT_LOG_PATH = tmp / "bot.log"

                vibes.LOG_DIR.mkdir(parents=True, exist_ok=True)
                stdout = tmp / "stdout.jsonl"
                stderr = tmp / "stderr.txt"
                stdout.write_text("x\n", encoding="utf-8")
                stderr.write_text("y\n", encoding="utf-8")

                extra1 = vibes.LOG_DIR / "S_20200101_000000.jsonl"
                extra2 = vibes.LOG_DIR / "S_20200101_000000.stderr.txt"
                extra1.write_text("a\n", encoding="utf-8")
                extra2.write_text("b\n", encoding="utf-8")

                rec = vibes.SessionRecord(name="S", path=str(tmp / "project"))
                rec.last_stdout_log = str(stdout)
                rec.last_stderr_log = str(stderr)

                manager = vibes.SessionManager(admin_id=1)
                manager._delete_session_artifacts(rec)

                self.assertFalse(stdout.exists())
                self.assertFalse(stderr.exists())
                self.assertFalse(extra1.exists())
                self.assertFalse(extra2.exists())
            finally:
                vibes.STATE_PATH = old_state_path
                vibes.LOG_DIR = old_log_dir
                vibes.BOT_LOG_PATH = old_bot_log_path
