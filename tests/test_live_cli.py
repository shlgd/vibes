import json
import os
import subprocess
import unittest

import telegram_stubs

telegram_stubs.install()

import vibes  # noqa: E402


RUN_LIVE = os.environ.get("RUN_LIVE_CLI_TESTS") == "1"


def _iter_json_lines(output: str) -> list[dict]:
    items: list[dict] = []
    for line in output.splitlines():
        s = line.strip()
        if not s or not s.startswith("{"):
            continue
        try:
            obj = json.loads(s)
        except Exception:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    return items


@unittest.skipUnless(RUN_LIVE, "Set RUN_LIVE_CLI_TESTS=1 to run live CLI tests.")
class LiveCLITests(unittest.TestCase):
    def test_claude_stream_json(self) -> None:
        cmd = [
            "claude",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--permission-mode",
            os.environ.get("VIBES_CLAUDE_PERMISSION_MODE", "bypassPermissions"),
            "--model",
            os.environ.get("VIBES_CLAUDE_MODEL", "sonnet"),
            "Say OK.",
        ]
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=180)
        items = _iter_json_lines(out)
        self.assertTrue(items)
        self.assertTrue(any(obj.get("type") == "result" for obj in items))

    def test_codex_exec_json(self) -> None:
        sandbox = os.environ.get("VIBES_CODEX_SANDBOX", "danger-full-access")
        approval = os.environ.get("VIBES_CODEX_APPROVAL_POLICY", "never")
        rec = vibes.SessionRecord(name="S", path=os.getcwd())
        manager = vibes.SessionManager(admin_id=None)
        cmd = manager._build_codex_cmd(rec, prompt="Say OK.", run_mode="new")
        # Ensure desired sandbox/approval are in the command
        if "--sandbox" in cmd:
            idx = cmd.index("--sandbox")
            cmd[idx + 1] = sandbox
        if "-c" in cmd:
            for i, part in enumerate(cmd):
                if part == "-c" and i + 1 < len(cmd) and part != "":
                    if cmd[i + 1].startswith("approval_policy="):
                        cmd[i + 1] = f"approval_policy={approval}"
                        break
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=180)
        items = _iter_json_lines(out)
        self.assertTrue(items)
        self.assertTrue(any("type" in obj for obj in items))
