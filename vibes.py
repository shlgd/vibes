#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
vibes.py — Telegram-бот “session manager” для Codex CLI.

Запуск:
  python vibes.py --token <YOUR_BOT_TOKEN> --admin <YOUR_USER_ID>
  # или через env:
  VIBES_TOKEN="<YOUR_BOT_TOKEN>" python vibes.py

Фоновый режим (удобно из любого терминала):
  ./vibes init
  ./vibes start
  ./vibes status
  ./vibes stop

Зависимости:
  pip install "python-telegram-bot>=20,<22"
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import dataclasses
import datetime as dt
import html
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple


try:
    from telegram import Update
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    from telegram.error import BadRequest, RetryAfter, TelegramError
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except Exception as exc:  # noqa: BLE001 - хотим дружелюбный рантайм-эррор
    print(
        "Не удалось импортировать python-telegram-bot.\n"
        "Установи зависимость:\n"
        '  pip install "python-telegram-bot>=20,<22"\n'
        f"Оригинальная ошибка: {exc}",
        file=sys.stderr,
    )
    sys.exit(1)


DEFAULT_RUNTIME_DIR = Path("./.vibes")
DEFAULT_STATE_PATH = DEFAULT_RUNTIME_DIR / "vibe_state.json"
DEFAULT_LOG_DIR = DEFAULT_RUNTIME_DIR / "vibe_logs"
DEFAULT_BOT_LOG_PATH = DEFAULT_RUNTIME_DIR / "vibe_bot.log"

# NOTE: tests monkeypatch these module-level paths.
STATE_PATH = DEFAULT_STATE_PATH
LOG_DIR = DEFAULT_LOG_DIR
BOT_LOG_PATH = DEFAULT_BOT_LOG_PATH

# Legacy (pre-.vibes) locations.
LEGACY_STATE_PATH = Path("./vibe_state.json")
LEGACY_LOG_DIR = Path("./vibe_logs")
LEGACY_BOT_LOG_PATH = Path("./vibe_bot.log")

STATE_VERSION = 4

MAX_TELEGRAM_CHARS = 4096
EDIT_THROTTLE_SECONDS = 2.0
STDERR_TAIL_LINES = 80
UI_PREVIEW_MAX_CHARS = 2400
UI_TAIL_MAX_BYTES = 64 * 1024

MEDIA_GROUP_DEBOUNCE_SECONDS = 0.8
MAX_DOWNLOADED_FILENAME_LEN = 180

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

CB_PREFIX = "v3"

DEFAULT_MODEL_PRESETS: List[str] = [
    # Keep this list short; prefer reading the user's Codex config below.
    "gpt-5.2-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
    "gpt-5.2",
]

DEFAULT_MODEL = "gpt-5.2"
DEFAULT_REASONING_EFFORT = "high"

CODEX_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
CODEX_APPROVAL_POLICIES = {"untrusted", "on-failure", "on-request", "never"}

ENGINE_CODEX = "codex"
ENGINE_CLAUDE = "claude"
ENGINE_CHOICES = {ENGINE_CODEX, ENGINE_CLAUDE}

DEFAULT_CLAUDE_MODEL = "sonnet"
DEFAULT_CLAUDE_PERMISSION_MODE = "bypassPermissions"


def _env_flag(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _read_toml(path: Path) -> Optional[Dict[str, Any]]:
    try:
        import tomllib  # py3.11+
    except Exception:
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _discover_model_presets() -> List[str]:
    presets: List[str] = []
    seen: set[str] = set()

    allowed = set(DEFAULT_MODEL_PRESETS)

    def add(val: Optional[str]) -> None:
        if not isinstance(val, str):
            return
        s = val.strip()
        if not s or s in seen:
            return
        if allowed and s not in allowed:
            return
        seen.add(s)
        presets.append(s)

    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    cfg = codex_home / "config.toml"
    data = _read_toml(cfg)
    if isinstance(data, dict):
        model = data.get("model")
        if isinstance(model, str):
            add(model)

        notice = data.get("notice")
        if isinstance(notice, dict):
            migrations = notice.get("model_migrations")
            if isinstance(migrations, dict):
                if isinstance(model, str):
                    add(migrations.get(model) if isinstance(migrations.get(model), str) else None)

    for m in DEFAULT_MODEL_PRESETS:
        add(m)

    return presets


MODEL_PRESETS: List[str] = _discover_model_presets()

LABEL_BACK = "⬅️"
LABEL_LOG = "📜"
LABEL_START = "🚀"

RUN_START_WAIT_NOTE = (
    "The request has been sent. During startup (especially for larger models), the first logs may appear after about one minute — please wait…"
)


def _codex_sandbox_mode() -> str:
    raw = os.environ.get("VIBES_CODEX_SANDBOX", "").strip()
    if raw in CODEX_SANDBOX_MODES:
        return raw
    return "workspace-write"


def _codex_approval_policy() -> str:
    raw = os.environ.get("VIBES_CODEX_APPROVAL_POLICY", "").strip()
    if raw in CODEX_APPROVAL_POLICIES:
        return raw
    return "never"


def _claude_permission_mode() -> str:
    raw = os.environ.get("VIBES_CLAUDE_PERMISSION_MODE", "").strip()
    return raw or DEFAULT_CLAUDE_PERMISSION_MODE


def _claude_model_default() -> str:
    raw = os.environ.get("VIBES_CLAUDE_MODEL", "").strip()
    return raw or DEFAULT_CLAUDE_MODEL


def _detect_git_dir(path: Path) -> Optional[Path]:
    """
    Best-effort: return absolute path to the git directory for `path` (usually `.git`).
    Works for:
      - repo root with `.git/`
      - worktrees/submodules with `.git` file pointing to `gitdir: ...`
      - nested paths inside a repo (via `git rev-parse --git-dir`)
    """
    try:
        candidate = path / ".git"
    except Exception:
        candidate = None

    if candidate is not None and candidate.is_dir():
        return candidate.resolve()

    if candidate is not None and candidate.is_file():
        try:
            raw = candidate.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            raw = ""
        if raw.lower().startswith("gitdir:"):
            gitdir_str = raw.split(":", 1)[1].strip()
            if gitdir_str:
                gitdir_path = Path(gitdir_str).expanduser()
                if not gitdir_path.is_absolute():
                    gitdir_path = (path / gitdir_path).resolve()
                else:
                    gitdir_path = gitdir_path.resolve()
                if gitdir_path.exists():
                    return gitdir_path

    try:
        out = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None
    if not out:
        return None
    gitdir_path = Path(out).expanduser()
    if not gitdir_path.is_absolute():
        gitdir_path = (path / gitdir_path).resolve()
    else:
        gitdir_path = gitdir_path.resolve()
    return gitdir_path


def _log_line(message: str) -> None:
    line = f"[{_utc_now_iso()}] {message}\n"
    try:
        BOT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with BOT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        print(line, file=sys.stderr)


def _log_error(msg: str, exc: Optional[BaseException] = None) -> None:
    tail = ""
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        tail = f"\n{tb}"
    _log_line(f"{msg}{tail}")


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_session_name(name: str) -> Optional[str]:
    name = name.strip()
    if not name:
        return None
    if len(name) > 64:
        return None
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", name):
        return None
    return name


def _can_create_directory(path: Path) -> bool:
    """
    Best-effort check whether `path` (which does not exist yet) can likely be created.
    """
    try:
        if path.exists():
            return False
    except Exception:
        return False

    parent = path.parent
    while True:
        try:
            if parent.exists():
                if not parent.is_dir():
                    return False
                return bool(os.access(parent, os.W_OK | os.X_OK))
        except Exception:
            return False

        if parent.parent == parent:
            return False
        parent = parent.parent


def _safe_resolve_path(raw: str) -> Tuple[Optional[Path], str]:
    raw_s = (raw or "").strip()
    if not raw_s:
        return None, "Empty path."
    try:
        p = Path(raw_s).expanduser()
    except Exception as e:
        return None, f"Invalid path: {raw_s!r} ({e})"
    try:
        return p.resolve(), ""
    except Exception as e:
        return None, f"Failed to resolve path: {raw_s!r} ({e})"


def _pretty_path(path: Path) -> str:
    try:
        p = path.expanduser().resolve()
        home = Path.home().expanduser().resolve()
        if p == home:
            return "~"
        if home in p.parents:
            return f"~/{p.relative_to(home)}"
        return str(p)
    except Exception:
        return str(path)


def _default_projects_root() -> Path:
    raw = os.environ.get("VIBES_DEFAULT_PROJECTS_DIR", "").strip()
    if raw:
        try:
            p = Path(raw).expanduser()
        except Exception:
            return (Path.home() / "Documents").expanduser()
        try:
            return p.resolve()
        except Exception:
            return p
    return (Path.home() / "Documents").expanduser()


def _is_simple_folder_name(text: str) -> bool:
    s = (text or "").strip()
    if not s or s in {".", ".."}:
        return False
    if "\x00" in s:
        return False
    if "/" in s or "\\" in s:
        return False
    if s.startswith("~"):
        return False
    if re.match(r"^[a-zA-Z]:", s):
        return False
    return True


def _max_attachment_bytes() -> Optional[int]:
    raw = os.environ.get("VIBES_MAX_ATTACHMENT_MB", "").strip()
    if not raw:
        return None
    try:
        mb = int(raw)
    except Exception:
        return None
    if mb <= 0:
        return None
    return mb * 1024 * 1024


def _sanitize_attachment_basename(name: str) -> str:
    # Avoid path traversal and platform-specific path separators.
    base = (name or "").strip().replace("\x00", "")
    base = base.replace("/", "_").replace("\\", "_")
    base = "".join(ch if (ch >= " " and ch != "\x7f") else "_" for ch in base).strip()
    if not base or base in {".", ".."}:
        return "file"

    if len(base) > MAX_DOWNLOADED_FILENAME_LEN:
        p = Path(base)
        suffix = p.suffix
        if suffix and len(suffix) < MAX_DOWNLOADED_FILENAME_LEN:
            keep = MAX_DOWNLOADED_FILENAME_LEN - len(suffix)
            base = p.stem[:keep] + suffix
        else:
            base = base[:MAX_DOWNLOADED_FILENAME_LEN]
    return base


def _pick_unique_dest_path(dest_dir: Path, basename: str) -> Path:
    safe = _sanitize_attachment_basename(basename)
    cand = dest_dir / safe
    if not cand.exists():
        return cand

    p = Path(safe)
    stem = p.stem or "file"
    suffix = p.suffix
    for i in range(2, 10_000):
        cand2 = dest_dir / f"{stem}_{i}{suffix}"
        if not cand2.exists():
            return cand2

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    return dest_dir / f"{stem}_{ts}{suffix}"


@dataclasses.dataclass(frozen=True)
class _AttachmentRef:
    file_id: str
    file_unique_id: Optional[str]
    preferred_name: Optional[str]
    default_stem: str
    file_size: Optional[int]


def _extract_message_attachments(message: Any) -> List[_AttachmentRef]:
    """
    Best-effort extraction of file-like Telegram attachments from a message.
    Returns a list to support media groups (each message usually has one attachment).
    """
    att = getattr(message, "effective_attachment", None)
    if not att:
        return []

    # Photos come as a list of sizes; pick the biggest.
    if isinstance(att, list):
        if not att:
            return []
        best = att[-1]
        file_id = getattr(best, "file_id", None)
        if not isinstance(file_id, str) or not file_id:
            return []
        unique = getattr(best, "file_unique_id", None)
        uniq = unique if isinstance(unique, str) and unique else None
        size = getattr(best, "file_size", None)
        file_size = int(size) if isinstance(size, int) and size > 0 else None
        stem = f"photo_{uniq or file_id}"
        return [
            _AttachmentRef(
                file_id=file_id,
                file_unique_id=uniq,
                preferred_name=None,
                default_stem=stem,
                file_size=file_size,
            )
        ]

    file_id = getattr(att, "file_id", None)
    if not isinstance(file_id, str) or not file_id:
        return []
    unique = getattr(att, "file_unique_id", None)
    uniq = unique if isinstance(unique, str) and unique else None

    preferred = getattr(att, "file_name", None)
    preferred_name = preferred if isinstance(preferred, str) and preferred.strip() else None
    size = getattr(att, "file_size", None)
    file_size = int(size) if isinstance(size, int) and size > 0 else None

    # Derive a stable-ish stem from attachment "type".
    type_hint = "file"
    for attr, hint in (
        ("document", "document"),
        ("audio", "audio"),
        ("video", "video"),
        ("voice", "voice"),
        ("video_note", "video_note"),
        ("animation", "animation"),
        ("sticker", "sticker"),
    ):
        if getattr(message, attr, None) is att:
            type_hint = hint
            break

    stem = f"{type_hint}_{uniq or file_id}"
    return [
        _AttachmentRef(
            file_id=file_id,
            file_unique_id=uniq,
            preferred_name=preferred_name,
            default_stem=stem,
            file_size=file_size,
        )
    ]


async def _download_attachments_to_session_root(
    *,
    message: Any,
    bot: Any,
    session_root: Path,
) -> Tuple[List[str], Optional[str]]:
    if not session_root.exists() or not session_root.is_dir():
        raise FileNotFoundError(f"Session directory not found: {session_root}")

    refs = _extract_message_attachments(message)
    if not refs:
        return [], None

    saved: List[str] = []
    skipped: List[str] = []
    max_bytes = _max_attachment_bytes()
    for ref in refs:
        if max_bytes is not None and isinstance(ref.file_size, int) and ref.file_size > max_bytes:
            label = ref.preferred_name or f"{ref.default_stem} (id:{ref.file_id})"
            skipped.append(label)
            continue

        tg_file = await bot.get_file(ref.file_id)
        file_path = getattr(tg_file, "file_path", None)
        suffix = ""
        if isinstance(file_path, str) and file_path:
            suffix = Path(file_path).suffix
        if not suffix:
            suffix = ""

        preferred = ref.preferred_name
        if preferred is None:
            preferred = f"{ref.default_stem}{suffix}"

        dest_path = _pick_unique_dest_path(session_root, preferred)
        await tg_file.download_to_drive(custom_path=str(dest_path))
        saved.append(dest_path.name)

    notice = None
    if skipped and max_bytes is not None:
        lim_mb = max_bytes / (1024 * 1024)
        skipped_view = ", ".join(skipped[:6])
        more = f" (+{len(skipped) - 6} more)" if len(skipped) > 6 else ""
        notice = f"Attachment too large (limit: {lim_mb:.0f} MB). Skipped: {skipped_view}{more}"

    if saved:
        saved_view = ", ".join(saved[:6])
        more = f" (+{len(saved) - 6} more)" if len(saved) > 6 else ""
        _log_line(f"attachments_saved dir={session_root} files={saved_view}{more}")
    if notice:
        _log_line(f"attachments_skipped dir={session_root} reason={notice}")

    return saved, notice


def _build_prompt_with_downloaded_files(*, user_text: str, filenames: List[str]) -> str:
    names = [n for n in (filenames or []) if isinstance(n, str) and n.strip()]
    names = sorted(set(names))
    file_list = "\n".join(f"- {n}" for n in names) if names else "- (нет)"
    user_text = (user_text or "").strip()

    if user_text:
        return (
            "В корне рабочей директории этой сессии сохранены файлы (скачаны из Telegram).\n"
            "Обрати на них внимание и в ответе перечисли их имена списком:\n"
            f"{file_list}\n\n"
            "Сообщение пользователя:\n"
            f"{user_text}"
        ).strip()

    return (
        "В корне рабочей директории этой сессии сохранены файлы (скачаны из Telegram).\n"
        "Обрати на них внимание и в ответе перечисли их имена списком:\n"
        f"{file_list}\n\n"
        "Текущего текста от пользователя нет.\n"
        "Если задача/промпт находится в этих файлах (текст, PDF, изображения и т.п.) — извлеки его и выполни."
    ).strip()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def _rewrite_legacy_log_path(path_str: str) -> str:
    if not path_str:
        return path_str
    try:
        p = Path(path_str)
    except Exception:
        return path_str

    candidates: List[Path] = [LEGACY_LOG_DIR]
    try:
        candidates.append(LEGACY_LOG_DIR.resolve())
    except Exception:
        pass
    try:
        candidates.append((Path.cwd() / LEGACY_LOG_DIR).resolve())
    except Exception:
        pass

    for base in candidates:
        try:
            rel = p.relative_to(base)
        except Exception:
            continue
        return str(LOG_DIR / rel)
    return path_str


def _rewrite_state_paths_for_runtime_dir(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    sessions = raw.get("sessions")
    if not isinstance(sessions, dict):
        return raw, False

    changed = False
    for payload in sessions.values():
        if not isinstance(payload, dict):
            continue
        for key in ("last_stdout_log", "last_stderr_log"):
            val = payload.get(key)
            if not isinstance(val, str) or not val:
                continue
            rewritten = _rewrite_legacy_log_path(val)
            if rewritten != val:
                payload[key] = rewritten
                changed = True

    return raw, changed


def _maybe_migrate_runtime_files() -> None:
    """
    Best-effort migration to keep all runtime state under `.vibes/`.
    Skips migration if paths were monkeypatched (e.g. in tests).
    """
    if STATE_PATH != DEFAULT_STATE_PATH or LOG_DIR != DEFAULT_LOG_DIR or BOT_LOG_PATH != DEFAULT_BOT_LOG_PATH:
        return

    try:
        DEFAULT_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    if LEGACY_BOT_LOG_PATH.exists() and not BOT_LOG_PATH.exists():
        try:
            LEGACY_BOT_LOG_PATH.rename(BOT_LOG_PATH)
        except Exception:
            pass

    if LEGACY_LOG_DIR.exists() and not LOG_DIR.exists():
        try:
            LEGACY_LOG_DIR.rename(LOG_DIR)
        except Exception:
            pass

    if LEGACY_STATE_PATH.exists() and not STATE_PATH.exists():
        raw: Any
        try:
            raw = json.loads(LEGACY_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw = None

        if isinstance(raw, dict):
            raw, _changed = _rewrite_state_paths_for_runtime_dir(raw)
            try:
                _atomic_write_text(STATE_PATH, json.dumps(raw, ensure_ascii=False, indent=2))
                LEGACY_STATE_PATH.unlink(missing_ok=True)
                return
            except Exception:
                pass

        try:
            LEGACY_STATE_PATH.rename(STATE_PATH)
        except Exception:
            pass

    if STATE_PATH.exists():
        raw2: Any
        try:
            raw2 = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw2 = None
        if isinstance(raw2, dict):
            raw2, changed2 = _rewrite_state_paths_for_runtime_dir(raw2)
            if changed2:
                try:
                    _atomic_write_text(STATE_PATH, json.dumps(raw2, ensure_ascii=False, indent=2))
                except Exception:
                    pass


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = max(0, (limit // 2) - 10)
    tail = max(0, limit - head - 20)
    return f"{text[:head]}\n…(обрезано)…\n{text[-tail:]}"


def _strip_html_tags(text_html: str) -> str:
    raw = text_html or ""
    try:
        raw = re.sub(r"<[^>]+>", "", raw)
    except Exception:
        pass
    try:
        return html.unescape(raw)
    except Exception:
        return raw


def _telegram_safe_html_code_block(text: str, *, max_chars: int = MAX_TELEGRAM_CHARS) -> str:
    plain_budget = max(200, max_chars - 50)
    for _ in range(12):
        plain_view = (text or "").strip()
        if len(plain_view) > plain_budget:
            plain_view = _truncate_text(plain_view, plain_budget)
        candidate = f"<pre><code>{html.escape(plain_view)}</code></pre>"
        if len(candidate) <= max_chars:
            return candidate
        plain_budget = max(200, int(plain_budget * 0.7))
    plain_view = _truncate_text((text or "").strip(), max(200, max_chars // 2))
    return f"<pre><code>{html.escape(plain_view)}</code></pre>"


def _tail_text(text: str, limit: int, *, prefix: str = "…") -> str:
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(prefix))
    if keep <= 0:
        return text[-limit:]
    return prefix + text[-keep:]


def _format_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s"


def _looks_like_uuid(value: Any) -> Optional[str]:
    if isinstance(value, str):
        m = UUID_RE.search(value)
        if m:
            return m.group(0)
    return None


def _find_first_uuid(obj: Any, max_depth: int = 6) -> Optional[str]:
    seen: set[int] = set()

    def walk(node: Any, depth: int) -> Optional[str]:
        if depth > max_depth:
            return None
        node_id = id(node)
        if node_id in seen:
            return None
        seen.add(node_id)

        uuid_val = _looks_like_uuid(node)
        if uuid_val:
            return uuid_val

        if isinstance(node, dict):
            for key in ("session_id", "thread_id", "id"):
                if key in node:
                    uuid_val2 = _looks_like_uuid(node.get(key))
                    if uuid_val2:
                        return uuid_val2
            for val in node.values():
                found = walk(val, depth + 1)
                if found:
                    return found
            return None

        if isinstance(node, list):
            for val in node:
                found = walk(val, depth + 1)
                if found:
                    return found
            return None

        return None

    return walk(obj, 0)


def _extract_session_id_explicit(obj: Dict[str, Any]) -> Optional[str]:
    candidates: List[Any] = []
    candidates.extend([obj.get("session_id"), obj.get("thread_id")])

    thread = obj.get("thread")
    if isinstance(thread, dict):
        candidates.append(thread.get("id"))
    session = obj.get("session")
    if isinstance(session, dict):
        candidates.append(session.get("id"))

    data = obj.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("session_id"), data.get("thread_id")])
        thread2 = data.get("thread")
        if isinstance(thread2, dict):
            candidates.append(thread2.get("id"))
        session2 = data.get("session")
        if isinstance(session2, dict):
            candidates.append(session2.get("id"))

    for cand in candidates:
        uuid_val = _looks_like_uuid(cand)
        if uuid_val:
            return uuid_val
    return None


def _get_event_type(obj: Dict[str, Any]) -> str:
    for key in ("type", "event", "kind", "name"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_text_delta(obj: Dict[str, Any]) -> Optional[str]:
    # На практике у разных версий/провайдеров поля могут отличаться.
    for key in ("delta", "text", "content"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val
    # Вариант вида: {"data": {"text": "..."}}
    data = obj.get("data")
    if isinstance(data, dict):
        for key in ("delta", "text", "content"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _extract_claude_session_id(obj: Dict[str, Any]) -> Optional[str]:
    if obj.get("type") != "system" or obj.get("subtype") != "init":
        return None
    session_id = obj.get("session_id")
    return session_id if isinstance(session_id, str) and session_id.strip() else None


def _extract_claude_text_delta(obj: Dict[str, Any]) -> Optional[str]:
    if obj.get("type") != "stream_event":
        return None
    event = obj.get("event")
    if not isinstance(event, dict):
        return None
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None
    if delta.get("type") != "text_delta":
        return None
    text = delta.get("text")
    return text if isinstance(text, str) and text else None


def _extract_claude_assistant_text(obj: Dict[str, Any]) -> Optional[str]:
    if obj.get("type") != "assistant":
        return None
    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    if not parts:
        return None
    return "".join(parts)


def _extract_claude_result_text(obj: Dict[str, Any]) -> Optional[str]:
    if obj.get("type") != "result":
        return None
    result = obj.get("result")
    return result if isinstance(result, str) and result.strip() else None


def _extract_item(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    item = obj.get("item")
    if isinstance(item, dict):
        return item
    data = obj.get("data")
    if isinstance(data, dict):
        item2 = data.get("item")
        if isinstance(item2, dict):
            return item2
    return None


def _extract_item_type(item: Dict[str, Any]) -> str:
    val = item.get("type")
    return val.strip() if isinstance(val, str) else ""


def _extract_item_text(item: Dict[str, Any]) -> Optional[str]:
    for key in ("delta", "text", "content"):
        val = item.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _extract_tool_command(obj: Dict[str, Any]) -> Optional[str]:
    # Ожидаем что-то вроде:
    # {"type":"tool_use","name":"shell_command","input":{"command":"ls"}} или варианты.
    for key in ("command", "cmd"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    data = obj.get("data")
    if isinstance(data, dict):
        for key in ("command", "cmd"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

    tool_input = obj.get("input")
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command")
        if isinstance(cmd, str) and cmd.strip():
            return cmd.strip()
    if isinstance(data, dict):
        tool_input2 = data.get("input")
        if isinstance(tool_input2, dict):
            cmd = tool_input2.get("command")
            if isinstance(cmd, str) and cmd.strip():
                return cmd.strip()

    return None


def _extract_tool_output(obj: Dict[str, Any]) -> Optional[str]:
    for key in ("output", "stdout", "result", "text"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val
    data = obj.get("data")
    if isinstance(data, dict):
        for key in ("output", "stdout", "result", "text"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _maybe_extract_diff(obj: Dict[str, Any]) -> Optional[str]:
    for key in ("diff", "patch", "unified_diff"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val
    data = obj.get("data")
    if isinstance(data, dict):
        for key in ("diff", "patch", "unified_diff"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val
    return None


def _cb(*parts: str) -> str:
    safe_parts = [CB_PREFIX]
    for p in parts:
        safe_parts.append(p.replace(":", "_"))
    return ":".join(safe_parts)


def _tail_text_file(path: Path, *, max_bytes: int = UI_TAIL_MAX_BYTES) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        to_read = min(size, max_bytes)
        with path.open("rb") as f:
            if to_read < size:
                f.seek(-to_read, os.SEEK_END)
            data = f.read(to_read)
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_last_agent_message_from_stdout_log(path: Optional[str], *, max_chars: int = UI_PREVIEW_MAX_CHARS) -> str:
    if not path:
        return ""
    p = Path(path)
    raw = _tail_text_file(p)
    if not raw.strip():
        return ""

    for line in reversed(raw.splitlines()[-500:]):
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        claude_text = _extract_claude_assistant_text(obj) or _extract_claude_result_text(obj)
        if claude_text:
            return _truncate_text(claude_text.strip(), max_chars)
        event_type = _get_event_type(obj)
        if event_type in {"agent_message", "assistant_message"}:
            text = obj.get("text")
            if isinstance(text, str) and text.strip():
                return _truncate_text(text.strip(), max_chars)
        if event_type.startswith("item."):
            item = _extract_item(obj)
            if isinstance(item, dict):
                item_type = _extract_item_type(item)
                if item_type in {"assistant_message", "message"}:
                    item_text = _extract_item_text(item)
                    if isinstance(item_text, str) and item_text.strip():
                        return _truncate_text(item_text.strip(), max_chars)
    return ""


def _preview_from_stdout_log(path: Optional[str], *, max_chars: int = UI_PREVIEW_MAX_CHARS) -> str:
    if not path:
        return ""
    p = Path(path)
    raw = _tail_text_file(p)
    if not raw.strip():
        return ""

    pieces: List[str] = []
    last_cmd: Optional[str] = None
    saw_claude_delta = False
    for line in raw.splitlines()[-250:]:
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except Exception:
            pieces.append(line)
            continue
        if not isinstance(obj, dict):
            pieces.append(line)
            continue

        delta = _extract_claude_text_delta(obj)
        if delta:
            pieces.append(delta)
            saw_claude_delta = True
            continue

        if not saw_claude_delta:
            claude_msg = _extract_claude_assistant_text(obj)
            if claude_msg:
                pieces.append("\n" + claude_msg + "\n")
                continue
            claude_result = _extract_claude_result_text(obj)
            if claude_result:
                pieces.append("\n" + claude_result + "\n")
                continue

        event_type = _get_event_type(obj)
        if event_type.startswith("item."):
            item = _extract_item(obj)
            if isinstance(item, dict):
                item_type = _extract_item_type(item)
                if item_type == "reasoning":
                    continue
                if item_type == "command_execution":
                    cmd = item.get("command")
                    out = item.get("aggregated_output")
                    exit_code = item.get("exit_code")
                    status = item.get("status")
                    is_start = event_type.endswith("started") or status == "in_progress"
                    is_done = event_type.endswith("completed") or status in {"completed", "failed"}

                    cmd_s = cmd.strip() if isinstance(cmd, str) else ""
                    if cmd_s and (is_start or is_done) and cmd_s != last_cmd:
                        pieces.append(f"\n$ {cmd_s}\n")
                        last_cmd = cmd_s
                    if is_done:
                        if isinstance(out, str) and out.strip():
                            pieces.append(_truncate_text(out, 800) + "\n")
                        if isinstance(exit_code, int):
                            pieces.append(f"(exit_code: {exit_code})\n")
                    continue

                item_text = _extract_item_text(item)
                if item_text:
                    pieces.append(item_text)
                    continue

        if event_type == "text":
            delta = _extract_text_delta(obj)
            if delta:
                pieces.append(delta)
            continue
        if event_type in {"agent_message", "assistant_message"}:
            msg = obj.get("text")
            if isinstance(msg, str) and msg:
                pieces.append("\n" + msg + "\n")
            continue
        if event_type == "tool_use":
            cmd = _extract_tool_command(obj) or ""
            pieces.append(f"\n[tool_use]\n{cmd}\n")
            continue
        if event_type == "tool_result":
            out = _extract_tool_output(obj) or ""
            pieces.append(f"\n[tool_result]\n{_truncate_text(out, 800)}\n")
            continue

        diff = _maybe_extract_diff(obj)
        if diff:
            pieces.append(f"\n[file_change]\n{_truncate_text(diff, 800)}\n")
            continue

        delta = _extract_text_delta(obj)
        if delta:
            pieces.append(delta)

    text = "".join(pieces).strip()
    return _truncate_text(text, max_chars)


def _preview_from_stderr_log(path: Optional[str], *, max_chars: int = 1200) -> str:
    if not path:
        return ""
    p = Path(path)
    raw = _tail_text_file(p)
    if not raw.strip():
        return ""
    tail = "\n".join(raw.splitlines()[-40:])
    return _truncate_text(tail, max_chars)


@dataclasses.dataclass
class Segment:
    kind: str  # "text" | "code"
    content: str

    def plain_len(self) -> int:
        return len(self.content)

    def render_html(self) -> str:
        if self.kind == "code":
            return f"<pre><code>{html.escape(self.content)}</code></pre>"
        return html.escape(self.content)


class TelegramStream:
    def __init__(
        self,
        application: Application,
        chat_id: int,
        message_id: int,
        *,
        header_html: str = "",
        header_plain_len: int = 0,
        auto_clear_header_on_first_log: bool = False,
        footer_provider: Optional["Callable[[], str]"] = None,
        footer_plain_len: int = 0,
        wrap_log_in_pre: bool = False,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        on_panel_replaced: Optional["Callable[[int], Awaitable[None]]"] = None,
    ) -> None:
        self._app = application
        self._chat_id = chat_id
        self._message_id = message_id
        self._header_html = header_html
        self._header_plain_len = header_plain_len
        self._auto_clear_header_on_first_log = auto_clear_header_on_first_log
        self._footer_provider = footer_provider
        self._footer_plain_len = footer_plain_len
        self._wrap_log_in_pre = wrap_log_in_pre
        self._reply_markup = reply_markup
        self._on_panel_replaced = on_panel_replaced
        self._log_segments: List[Segment] = []
        self._lock = asyncio.Lock()
        self._dirty = asyncio.Event()
        self._stop = asyncio.Event()
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._task: asyncio.Task[None] = asyncio.create_task(self._run())
        self._last_edit_mono = 0.0
        self._last_sent_html: Optional[str] = None
        self._last_sent_markup: Optional[InlineKeyboardMarkup] = None
        self._dirty.set()

    async def set_header(self, *, header_html: str, header_plain_len: Optional[int] = None) -> None:
        async with self._lock:
            self._header_html = header_html
            if header_plain_len is not None:
                self._header_plain_len = header_plain_len
            else:
                # Грубая оценка, чтобы распределять бюджет под tail-лог.
                self._header_plain_len = len(re.sub(r"<[^>]+>", "", header_html))
        self._dirty.set()

    async def set_reply_markup(self, reply_markup: Optional[InlineKeyboardMarkup]) -> None:
        async with self._lock:
            self._reply_markup = reply_markup
        self._dirty.set()

    async def set_footer(
        self,
        *,
        footer_provider: Optional["Callable[[], str]"],
        footer_plain_len: Optional[int] = None,
        wrap_log_in_pre: Optional[bool] = None,
    ) -> None:
        async with self._lock:
            self._footer_provider = footer_provider
            if footer_plain_len is not None:
                self._footer_plain_len = footer_plain_len
            else:
                sample = footer_provider() if footer_provider else ""
                self._footer_plain_len = len(re.sub(r"<[^>]+>", "", sample))
            if wrap_log_in_pre is not None:
                self._wrap_log_in_pre = wrap_log_in_pre
        self._dirty.set()

    def get_message_id(self) -> int:
        return self._message_id

    def get_chat_id(self) -> int:
        return self._chat_id

    async def add_text(self, text: str) -> None:
        if not text:
            return
        async with self._lock:
            if self._auto_clear_header_on_first_log:
                self._auto_clear_header_on_first_log = False
                self._header_html = ""
                self._header_plain_len = 0
            if self._log_segments and self._log_segments[-1].kind == "text":
                self._log_segments[-1].content += text
            else:
                self._log_segments.append(Segment(kind="text", content=text))
        self._dirty.set()

    async def add_code(self, code: str) -> None:
        if not code:
            return
        async with self._lock:
            if self._auto_clear_header_on_first_log:
                self._auto_clear_header_on_first_log = False
                self._header_html = ""
                self._header_plain_len = 0
            if not self._log_segments or not self._log_segments[-1].content.endswith("\n"):
                # разделяем визуально
                self._log_segments.append(Segment(kind="text", content="\n"))
            self._log_segments.append(Segment(kind="code", content=code))
            self._log_segments.append(Segment(kind="text", content="\n"))
        self._dirty.set()

    async def stop(self) -> None:
        self._stop.set()
        self._dirty.set()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def pause(self) -> None:
        self._resume_event.clear()

    async def resume(self) -> None:
        self._resume_event.set()
        self._dirty.set()

    async def _snapshot(
        self,
    ) -> Tuple[
        str,
        int,
        Optional["Callable[[], str]"],
        int,
        bool,
        Optional[InlineKeyboardMarkup],
        List[Segment],
    ]:
        async with self._lock:
            return (
                self._header_html,
                self._header_plain_len,
                self._footer_provider,
                self._footer_plain_len,
                self._wrap_log_in_pre,
                self._reply_markup,
                list(self._log_segments),
            )

    def _tail_segments(self, segments: List[Segment], max_plain: int) -> List[Segment]:
        total = 0
        kept_rev: List[Segment] = []
        for seg in reversed(segments):
            seg_len = seg.plain_len()
            if total + seg_len <= max_plain:
                kept_rev.append(seg)
                total += seg_len
                continue
            if not kept_rev:
                # Один сегмент слишком большой — оставляем хвост.
                kept_rev.append(Segment(kind=seg.kind, content=seg.content[-max_plain:]))
                total = max_plain
            break

        kept = list(reversed(kept_rev))
        if len(kept) < len(segments):
            prefix = Segment(
                kind="text",
                content="…previous output hidden…\n\n",
            )
            kept = [prefix] + kept
        return kept

    async def _render_html(self) -> str:
        (
            header_html,
            header_plain_len,
            footer_provider,
            footer_plain_len,
            wrap_log_in_pre,
            _reply_markup,
            log_segments,
        ) = await self._snapshot()

        footer_html = ""
        if footer_provider:
            try:
                footer_html = footer_provider() or ""
            except Exception:
                footer_html = ""

        header_html = header_html.strip()
        footer_html = footer_html.strip()

        # Оставляем небольшой запас на HTML-обвязку и неожиданные экранирования.
        max_plain_total = MAX_TELEGRAM_CHARS - 250
        if max_plain_total < 500:
            max_plain_total = MAX_TELEGRAM_CHARS

        max_plain_log = max_plain_total - header_plain_len - footer_plain_len - 50
        if max_plain_log < 300:
            max_plain_log = 300

        # Additional safety: HTML escaping can expand the payload beyond our "plain length" budget.
        # If Telegram rejects due to length, we progressively shrink the log tail budget.
        for _ in range(8):
            tail_segments = self._tail_segments(log_segments, max_plain=max_plain_log)
            if wrap_log_in_pre:
                plain_log = "".join(seg.content for seg in tail_segments).strip("\n")
                log_html = f"<pre><code>{html.escape(plain_log)}</code></pre>" if plain_log else "<pre><code></code></pre>"
            else:
                log_html = "".join(seg.render_html() for seg in tail_segments).strip()

            parts = [p for p in (header_html, log_html, footer_html) if p]
            text_html = "\n\n".join(parts)
            if len(text_html) <= MAX_TELEGRAM_CHARS:
                return text_html
            max_plain_log = max(80, int(max_plain_log * 0.75))

        parts = [p for p in (header_html, log_html, footer_html) if p]
        return "\n\n".join(parts)

    async def _edit(self, text_html: str, reply_markup: Optional[InlineKeyboardMarkup]) -> None:
        if text_html == self._last_sent_html and reply_markup == self._last_sent_markup:
            return
        attempts = 0
        delay_s = 0.0
        started_mono = time.monotonic()
        max_total_wait_s = 60.0 if self._stop.is_set() else 15.0
        max_attempts = 12 if self._stop.is_set() else 5

        while True:
            attempts += 1
            try:
                await self._app.bot.edit_message_text(
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    text=text_html,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=reply_markup,
                )
                self._last_sent_html = text_html
                self._last_sent_markup = reply_markup
                return
            except asyncio.CancelledError:
                raise
            except RetryAfter as e:
                retry_after = float(getattr(e, "retry_after", 2.0))
                if retry_after <= 0:
                    retry_after = 2.0
                delay_s = max(retry_after, delay_s * 2 if delay_s > 0 else retry_after)

                # During normal operation: don't block the whole stream for too long.
                # On shutdown/stop: give Telegram more time so the final render doesn't get lost.
                if attempts >= max_attempts or (time.monotonic() - started_mono) > max_total_wait_s:
                    if not self._stop.is_set():
                        self._dirty.set()
                    return

                await asyncio.sleep(delay_s)
                continue
            except BadRequest as e:
                msg = str(e).lower()
                if "message is not modified" in msg:
                    self._last_sent_html = text_html
                    self._last_sent_markup = reply_markup
                    return

                # Если по технической причине сообщение нельзя редактировать — не создаём новое сообщение.
                if (
                    "message can't be edited" in msg
                    or "message to edit not found" in msg
                    or "message_id_invalid" in msg
                    or "message to edit not found" in msg
                    or "chat not found" in msg
                ):
                    self._last_sent_html = text_html
                    self._last_sent_markup = reply_markup
                    return

                _log_error(f"Telegram edit failed (BadRequest): {msg}", e)
                raise

    async def _run(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()

            now = asyncio.get_running_loop().time()
            wait = max(0.0, EDIT_THROTTLE_SECONDS - (now - self._last_edit_mono))
            if wait > 0 and not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass

            if not self._resume_event.is_set():
                resume_task = asyncio.create_task(self._resume_event.wait())
                stop_task = asyncio.create_task(self._stop.wait())
                done, _ = await asyncio.wait({resume_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                for task in (resume_task, stop_task):
                    if task not in done:
                        task.cancel()
                if stop_task in done:
                    return

            text_html = await self._render_html()
            _header_html, _header_plain_len, _footer_provider, _footer_plain_len, _wrap_log_in_pre, reply_markup, _segments = (
                await self._snapshot()
            )
            try:
                await self._edit(text_html, reply_markup)
            except TelegramError:
                # Не валим весь прогон из-за ошибок Telegram — лог в stderr.
                print("Ошибка Telegram при редактировании сообщения", file=sys.stderr)
            self._last_edit_mono = asyncio.get_running_loop().time()

            if self._stop.is_set() and not self._dirty.is_set():
                return


@dataclasses.dataclass
class SessionRun:
    process: asyncio.subprocess.Process
    stdout_task: asyncio.Task[None]
    stderr_task: asyncio.Task[None]
    stream: TelegramStream
    stdout_log: Path
    stderr_log: Path
    stderr_tail: Deque[str]
    started_mono: float = dataclasses.field(default_factory=time.monotonic)
    last_cmd: Optional[str] = None
    stop_requested: bool = False
    confirm_stop: bool = False
    header_note: Optional[str] = None
    paused: bool = False


@dataclasses.dataclass
class SessionRecord:
    name: str
    path: str
    engine: str = ENGINE_CODEX
    thread_id: Optional[str] = None
    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    status: str = "idle"  # idle | running | error | stopped
    last_result: str = "never"  # never | success | error | stopped
    created_at: str = dataclasses.field(default_factory=_utc_now_iso)
    last_active: Optional[str] = None
    last_stdout_log: Optional[str] = None
    last_stderr_log: Optional[str] = None
    last_run_duration_s: Optional[int] = None
    pending_delete: bool = False
    run: Optional[SessionRun] = None


class SessionManager:
    def __init__(self, *, admin_id: Optional[int]) -> None:
        self._admin_id = admin_id
        self._state_lock = asyncio.Lock()
        self.sessions: Dict[str, SessionRecord] = {}
        self.panel_by_chat: Dict[int, int] = {}
        self._run_message_to_session: Dict[Tuple[int, int], str] = {}
        self.path_presets: List[str] = []
        self.owner_id: Optional[int] = None
        self._load_state()

    def register_run_message(self, *, chat_id: int, message_id: int, session_name: str) -> None:
        if chat_id and message_id and session_name:
            self._run_message_to_session[(chat_id, message_id)] = session_name

    def unregister_run_message(self, *, chat_id: int, message_id: int) -> None:
        self._run_message_to_session.pop((chat_id, message_id), None)

    def resolve_session_for_run_message(self, *, chat_id: int, message_id: int) -> Optional[str]:
        return self._run_message_to_session.get((chat_id, message_id))

    def resolve_attached_running_session_for_message(self, *, chat_id: int, message_id: int) -> Optional[str]:
        """
        Best-effort: multiple concurrent runs can target the same Telegram message id (the single "panel").
        In that case `_run_message_to_session` may become stale (e.g. after attaching to another session).

        Prefer the *currently attached* (not paused) running session whose stream is editing this
        (chat_id, message_id).
        """
        for name, rec in self.sessions.items():
            if not rec.run or rec.status != "running":
                continue
            try:
                if rec.run.stream.get_chat_id() != chat_id:
                    continue
                if rec.run.stream.get_message_id() != message_id:
                    continue
            except Exception:
                continue
            if rec.run.paused:
                continue
            return name
        return None

    async def pause_other_attached_runs(
        self,
        *,
        chat_id: int,
        message_id: int,
        except_session: Optional[str] = None,
    ) -> None:
        for name, rec in self.sessions.items():
            if except_session and name == except_session:
                continue
            if not rec.run or rec.status != "running":
                continue
            try:
                if rec.run.stream.get_chat_id() != chat_id:
                    continue
                if rec.run.stream.get_message_id() != message_id:
                    continue
            except Exception:
                continue
            if rec.run.paused:
                continue
            rec.run.paused = True
            await rec.run.stream.pause()

    async def ensure_owner(self, update: Update) -> bool:
        user = update.effective_user
        if not user:
            return False
        if self._admin_id is not None:
            return user.id == self._admin_id
        if self.owner_id is None:
            self.owner_id = user.id
            await self.save_state()
            return True
        return user.id == self.owner_id

    def _load_state(self) -> None:
        if not STATE_PATH.exists():
            return
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return

        sessions = raw.get("sessions", {})
        if isinstance(sessions, dict):
            for name, payload in sessions.items():
                if not isinstance(payload, dict):
                    continue
                safe_name = _safe_session_name(str(name))
                if not safe_name:
                    continue
                path = payload.get("path")
                if not isinstance(path, str) or not path:
                    continue
                engine_val = payload.get("engine")
                if not isinstance(engine_val, str) or engine_val not in ENGINE_CHOICES:
                    engine_val = ENGINE_CODEX
                model_val = payload.get("model")
                if not isinstance(model_val, str) or not model_val:
                    model_val = _claude_model_default() if engine_val == ENGINE_CLAUDE else DEFAULT_MODEL
                rec = SessionRecord(
                    name=safe_name,
                    path=path,
                    engine=engine_val,
                    thread_id=payload.get("thread_id")
                    if isinstance(payload.get("thread_id"), str)
                    else payload.get("session_id")
                    if isinstance(payload.get("session_id"), str)
                    else None,
                    model=model_val,
                    reasoning_effort=(
                        payload.get("reasoning_effort")
                        if isinstance(payload.get("reasoning_effort"), str) and payload.get("reasoning_effort")
                        else payload.get("model_reasoning_effort")
                        if isinstance(payload.get("model_reasoning_effort"), str) and payload.get("model_reasoning_effort")
                        else DEFAULT_REASONING_EFFORT
                    ),
                    status=payload.get("status") if isinstance(payload.get("status"), str) else "idle",
                    last_result=payload.get("last_result")
                    if isinstance(payload.get("last_result"), str) and payload.get("last_result") in {"never", "success", "error", "stopped"}
                    else "never",
                    created_at=payload.get("created_at") if isinstance(payload.get("created_at"), str) else _utc_now_iso(),
                    last_active=payload.get("last_active") if isinstance(payload.get("last_active"), str) else None,
                    last_stdout_log=payload.get("last_stdout_log") if isinstance(payload.get("last_stdout_log"), str) else None,
                    last_stderr_log=payload.get("last_stderr_log") if isinstance(payload.get("last_stderr_log"), str) else None,
                    last_run_duration_s=payload.get("last_run_duration_s")
                    if isinstance(payload.get("last_run_duration_s"), int)
                    else None,
                    pending_delete=payload.get("pending_delete") if isinstance(payload.get("pending_delete"), bool) else False,
                )
                if rec.last_stdout_log:
                    rec.last_stdout_log = _rewrite_legacy_log_path(rec.last_stdout_log)
                if rec.last_stderr_log:
                    rec.last_stderr_log = _rewrite_legacy_log_path(rec.last_stderr_log)
                # После рестарта процессов нет.
                if rec.status == "running":
                    rec.status = "idle"
                self.sessions[safe_name] = rec

        # Legacy: older versions persisted an "active_by_chat" (connected session) concept.
        # This is intentionally ignored now; UI state lives in per-chat `chat_data["ui"]`.

        panel = raw.get("panel_by_chat", {})
        if isinstance(panel, dict):
            for chat_id_str, msg_id in panel.items():
                try:
                    chat_id = int(chat_id_str)
                    message_id = int(msg_id)
                except Exception:
                    continue
                if chat_id and message_id:
                    self.panel_by_chat[chat_id] = message_id

        presets = raw.get("path_presets", [])
        if isinstance(presets, list):
            seen: set[str] = set()
            for p in presets:
                if not isinstance(p, str):
                    continue
                p2 = p.strip()
                if not p2 or p2 in seen:
                    continue
                seen.add(p2)
                self.path_presets.append(p2)

        owner_id = raw.get("owner_id")
        if isinstance(owner_id, int):
            self.owner_id = owner_id

    async def save_state(self) -> None:
        async with self._state_lock:
            payload = {
                "version": STATE_VERSION,
                "owner_id": self.owner_id,
                "sessions": {},
                "panel_by_chat": {str(k): v for k, v in self.panel_by_chat.items()},
                "path_presets": list(self.path_presets),
            }
            for name, rec in self.sessions.items():
                payload["sessions"][name] = {
                    "path": rec.path,
                    "engine": rec.engine,
                    "thread_id": rec.thread_id,
                    "model": rec.model,
                    "reasoning_effort": rec.reasoning_effort,
                    "status": rec.status if rec.status != "running" else "idle",
                    "last_result": rec.last_result,
                    "created_at": rec.created_at,
                    "last_active": rec.last_active,
                    "last_stdout_log": rec.last_stdout_log,
                    "last_stderr_log": rec.last_stderr_log,
                    "last_run_duration_s": rec.last_run_duration_s,
                    "pending_delete": rec.pending_delete,
                }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            await asyncio.to_thread(_atomic_write_text, STATE_PATH, text)

    def get_panel_message_id(self, chat_id: int) -> Optional[int]:
        return self.panel_by_chat.get(chat_id)

    async def set_panel_message_id(self, chat_id: int, message_id: int) -> None:
        self.panel_by_chat[chat_id] = message_id
        await self.save_state()

    async def upsert_path_preset(self, path: str) -> None:
        path = path.strip()
        if not path:
            return
        if path in self.path_presets:
            return
        self.path_presets.append(path)
        await self.save_state()

    async def delete_path_preset(self, index: int) -> bool:
        if index < 0 or index >= len(self.path_presets):
            return False
        self.path_presets.pop(index)
        await self.save_state()
        return True

    def next_auto_session_name(self) -> str:
        n = 1
        while True:
            cand = f"session-{n}"
            if cand not in self.sessions:
                return cand
            n += 1

    async def shutdown(self) -> None:
        # Останавливаем все активные прогоны.
        stop_tasks = []
        for name in list(self.sessions.keys()):
            rec = self.sessions.get(name)
            if rec and rec.run and rec.run.process.returncode is None:
                stop_tasks.append(self.stop(name, reason="shutdown"))
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)
        await self.save_state()

    async def create_session(
        self,
        *,
        name: str,
        path: str,
        engine: Optional[str] = None,
    ) -> Tuple[Optional[SessionRecord], str]:
        safe_name = _safe_session_name(name)
        if not safe_name:
            return None, "Invalid name. Allowed: a-zA-Z0-9._- (<=64)."

        engine_val = engine.strip() if isinstance(engine, str) else ""
        if not engine_val:
            engine_val = ENGINE_CODEX
        if engine_val not in ENGINE_CHOICES:
            return None, f"Invalid engine: {engine_val}"

        resolved, err = _safe_resolve_path(path)
        if err:
            return None, err
        abs_path = str(resolved)
        p = Path(abs_path)
        if not p.exists() or not p.is_dir():
            return None, f"Directory not found: {abs_path}"

        if safe_name in self.sessions:
            return None, f"Session '{safe_name}' already exists."

        model_val = _claude_model_default() if engine_val == ENGINE_CLAUDE else DEFAULT_MODEL
        rec = SessionRecord(
            name=safe_name,
            path=abs_path,
            engine=engine_val,
            model=model_val,
            status="idle",
            last_result="never",
        )
        self.sessions[safe_name] = rec
        await self.save_state()
        _log_line(f"session_created name={safe_name} engine={engine_val} path={abs_path}")
        return rec, ""

    async def delete_session(self, name: str) -> Tuple[bool, str]:
        rec = self.sessions.get(name)
        if not rec:
            return False, f"Unknown session: {name}"

        if rec.run and rec.run.process.returncode is None:
            rec.pending_delete = True
            await self.save_state()
            _log_line(f"session_delete_requested name={rec.name} engine={rec.engine}")
            await self.stop(name)
            return True, "Stop requested. Session will be deleted after it finishes."

        self._delete_session_artifacts(rec)
        del self.sessions[name]

        await self.save_state()
        _log_line(f"session_deleted name={rec.name} engine={rec.engine}")
        return True, "Deleted."

    async def clear_session_state(self, name: str) -> Tuple[bool, str]:
        rec = self.sessions.get(name)
        if not rec:
            return False, f"Unknown session: {name}"
        if rec.run and rec.run.process.returncode is None:
            return False, "This session is running."

        self._delete_session_artifacts(rec)
        rec.thread_id = None
        rec.status = "idle"
        rec.last_result = "never"
        rec.last_active = None
        rec.last_stdout_log = None
        rec.last_stderr_log = None
        rec.last_run_duration_s = None
        rec.pending_delete = False
        rec.run = None

        await self.save_state()
        _log_line(f"session_cleared name={rec.name} engine={rec.engine}")
        return True, "Cleared."

    def _delete_session_artifacts(self, rec: SessionRecord) -> None:
        # Удаляем только артефакты бота. Проектную директорию (rec.path) НЕ трогаем.
        seen: set[Path] = set()

        def add_path(p: Optional[str]) -> None:
            if not p:
                return
            seen.add(Path(p))

        add_path(rec.last_stdout_log)
        add_path(rec.last_stderr_log)

        if LOG_DIR.exists() and LOG_DIR.is_dir():
            for p in LOG_DIR.glob(f"{rec.name}_*.jsonl"):
                seen.add(p)
            for p in LOG_DIR.glob(f"{rec.name}_*.stderr.txt"):
                seen.add(p)

        for p in seen:
            try:
                if p.exists() and p.is_file():
                    p.unlink()
            except Exception:
                pass

    async def run_prompt(
        self,
        *,
        chat_id: int,
        panel_message_id: int,
        application: Application,
        session_name: str,
        prompt: str,
        run_mode: str,  # "continue" | "new"
    ) -> None:
        rec = self.sessions.get(session_name)
        if not rec:
            return

        if rec.run and rec.run.process.returncode is None:
            return

        if run_mode == "new":
            rec.thread_id = None
            await self.save_state()

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
        stdout_log = LOG_DIR / f"{rec.name}_{ts}.jsonl"
        stderr_log = LOG_DIR / f"{rec.name}_{ts}.stderr.txt"

        rec.status = "running"
        rec.last_active = _utc_now_iso()
        rec.last_stdout_log = str(stdout_log)
        rec.last_stderr_log = str(stderr_log)
        rec.last_run_duration_s = None
        await self.save_state()

        started_mono = time.monotonic()

        def _working_footer_html() -> str:
            elapsed_s = int(time.monotonic() - started_mono)
            return f"<code>---- Working {_h(_format_duration(elapsed_s))} ----</code>"

        running_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⬅️", callback_data=_cb("back_sessions")),
                    InlineKeyboardButton("⛔", callback_data=_cb("interrupt")),
                ]
            ]
        )

        # Only one run should be able to edit the panel at a time.
        try:
            await self.pause_other_attached_runs(chat_id=chat_id, message_id=panel_message_id, except_session=rec.name)
        except Exception as e:
            _log_error("pause_other_attached_runs failed.", e)

        engine = rec.engine if rec.engine in ENGINE_CHOICES else ENGINE_CODEX
        if engine == ENGINE_CLAUDE:
            cmd = self._build_claude_cmd(rec, prompt=prompt, run_mode=run_mode)
        else:
            cmd = self._build_codex_cmd(rec, prompt=prompt, run_mode=run_mode)
        run_cwd = rec.path if engine == ENGINE_CLAUDE else None
        _log_line(f"run_start session={rec.name} engine={engine} mode={run_mode} path={rec.path}")

        output_message_id = panel_message_id
        try:
            msg = await application.bot.send_message(
                chat_id=chat_id,
                text=f"<i>{_h(RUN_START_WAIT_NOTE)}</i>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            output_message_id = msg.message_id
        except Exception as e:
            _log_error("Failed to create output message; falling back to panel.", e)

        self.register_run_message(chat_id=chat_id, message_id=output_message_id, session_name=rec.name)

        stream = TelegramStream(
            application,
            chat_id=chat_id,
            message_id=output_message_id,
            header_html=f"<i>{_h(RUN_START_WAIT_NOTE)}</i>",
            header_plain_len=len(RUN_START_WAIT_NOTE),
            auto_clear_header_on_first_log=True,
            footer_provider=_working_footer_html,
            footer_plain_len=len("---- Working 0m 0s ----"),
            wrap_log_in_pre=True,
            reply_markup=running_kb,
        )

        async def _handle_start_failure(*, stderr_text: str) -> None:
            try:
                stderr_log.parent.mkdir(parents=True, exist_ok=True)
                stderr_log.write_text(stderr_text, encoding="utf-8")
            except Exception as e:
                _log_error("Failed to write stderr log for start failure.", e)
            rec.status = "error"
            rec.last_result = "error"
            rec.last_active = _utc_now_iso()
            rec.last_run_duration_s = int(time.monotonic() - started_mono)
            await self.save_state()
            await stream.stop()
            self.unregister_run_message(chat_id=chat_id, message_id=stream.get_message_id())
            try:
                panel = PanelUI(application, self)
                text_html, reply_markup = _render_session_view(self, session_name=rec.name, notice="Failed to start.")
                await panel.render_to_message(
                    chat_id=chat_id,
                    message_id=panel_message_id,
                    text_html=text_html,
                    reply_markup=reply_markup,
                    update_state_on_replace=True,
                )
            except Exception as e:
                _log_error("Failed to render start failure panel.", e)

        try:
            process = await self._spawn_process(cmd, cwd=run_cwd)
        except FileNotFoundError:
            missing = "`claude`" if engine == ENGINE_CLAUDE else "`codex`"
            await _handle_start_failure(stderr_text=f"{missing} not found in PATH.\n")
            return
        except Exception as e:
            name = "Claude" if engine == ENGINE_CLAUDE else "Codex"
            await _handle_start_failure(stderr_text=f"Failed to start {name}: {e}\n")
            return

        stderr_tail: Deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        stdout_task = asyncio.create_task(self._read_stdout(rec=rec, process=process, stream=stream, log_path=stdout_log))
        stderr_task = asyncio.create_task(self._read_stderr(process=process, log_path=stderr_log, stderr_tail=stderr_tail))

        rec.run = SessionRun(
            process=process,
            stdout_task=stdout_task,
            stderr_task=stderr_task,
            stream=stream,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            stderr_tail=stderr_tail,
            started_mono=started_mono,
        )
        await self.save_state()
        pid_val = getattr(process, "pid", None)
        pid_label = str(pid_val) if isinstance(pid_val, int) else "unknown"
        _log_line(f"run_spawned session={rec.name} engine={engine} pid={pid_label}")

        return_code = await process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        paused = bool(rec.run and rec.run.paused)
        rec.last_run_duration_s = int(time.monotonic() - started_mono)
        if rec.run and rec.run.stop_requested:
            rec.status = "stopped"
            rec.last_result = "stopped"
        elif return_code == 0:
            rec.status = "idle"
            rec.last_result = "success"
        else:
            rec.status = "error"
            rec.last_result = "error"

        rec.last_active = _utc_now_iso()
        await self.save_state()
        await stream.stop()
        self.unregister_run_message(chat_id=chat_id, message_id=stream.get_message_id())

        rec.run = None
        await self.save_state()
        _log_line(
            f"run_finished session={rec.name} engine={engine} code={return_code} status={rec.status} duration_s={rec.last_run_duration_s}"
        )

        if not paused:
            try:
                panel = PanelUI(application, self)
                text_html, reply_markup = _render_session_view(self, session_name=rec.name)
                await panel.render_to_message(
                    chat_id=chat_id,
                    message_id=panel_message_id,
                    text_html=text_html,
                    reply_markup=reply_markup,
                    update_state_on_replace=True,
                )
            except Exception:
                pass

        # Completion notice removed; info is available via the menu button.

        if rec.pending_delete:
            await self.delete_session(rec.name)

    async def _send_completion_notice(
        self,
        *,
        application: Any,
        chat_id: int,
        session_name: str,
        path: str,
        prompt: str,
    ) -> None:
        bot = getattr(application, "bot", None)
        send_message = getattr(bot, "send_message", None) if bot is not None else None
        if not callable(send_message):
            return

        prompt_clean = (prompt or "").strip() or "(empty)"
        prompt_max = 2400
        text_html = ""
        for _ in range(10):
            prompt_view = prompt_clean
            if len(prompt_view) > prompt_max:
                prompt_view = _truncate_text(prompt_view, prompt_max)

            parts = [
                "<b>Run finished</b>",
                f"Session: <code>{_h(session_name)}</code>",
                f"Path: <code>{_h(path)}</code>",
                "",
                "<b>Prompt:</b>",
                f"<pre><code>{_h(prompt_view)}</code></pre>",
            ]
            text_html = "\n".join([p for p in parts if p])
            if len(text_html) <= MAX_TELEGRAM_CHARS:
                break
            prompt_max = max(200, int(prompt_max * 0.7))

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅", callback_data=_cb("ack"))]])
        prompt_plain = _truncate_text(prompt_clean, 2000)
        text_plain = "\n".join(
            [
                "Run finished",
                f"Session: {session_name}",
                f"Path: {path}",
                "",
                "Prompt:",
                prompt_plain,
            ]
        ).strip()

        payloads: List[Dict[str, Any]] = [
            {
                "text": text_html,
                "parse_mode": ParseMode.HTML,
                "disable_web_page_preview": True,
                "reply_markup": kb,
            },
            {
                "text": _truncate_text(text_plain, 3500),
                "disable_web_page_preview": True,
                "reply_markup": kb,
            },
        ]

        for payload in payloads:
            delay_s = 1.0
            started_mono = time.monotonic()
            max_total_wait_s = 60.0 * 60.0
            remaining_attempts = 10
            while remaining_attempts > 0:
                try:
                    await send_message(chat_id=chat_id, **payload)
                    return
                except asyncio.CancelledError:
                    raise
                except RetryAfter as e:
                    retry_after = float(getattr(e, "retry_after", 2.0))
                    await asyncio.sleep(max(0.0, retry_after))
                    if (time.monotonic() - started_mono) > max_total_wait_s:
                        _log_error("Failed to send completion notice (RetryAfter timeout).")
                        break
                    continue
                except BadRequest as e:
                    _log_error("Failed to send completion notice (BadRequest).", e)
                    break
                except TelegramError as e:
                    remaining_attempts -= 1
                    if remaining_attempts <= 0 or (time.monotonic() - started_mono) > max_total_wait_s:
                        _log_error("Failed to send completion notice (TelegramError).", e)
                        break
                    await asyncio.sleep(delay_s)
                    delay_s = min(30.0, delay_s * 2)
                    continue
                except Exception as e:
                    _log_error("Failed to send completion notice (unexpected exception).", e)
                    break

    async def stop(self, name: str, *, reason: str = "user") -> bool:
        rec = self.sessions.get(name)
        if not rec or not rec.run:
            return False
        run = rec.run
        run.stop_requested = True
        _log_line(f"run_stop_requested session={rec.name} engine={rec.engine} reason={reason}")

        proc = run.process
        if proc.returncode is not None:
            return True

        try:
            if os.name == "posix":
                # Мы стартуем в отдельной сессии/группе — убиваем всю группу.
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except ProcessLookupError:
            return True
        except Exception:
            # Фоллбек
            try:
                proc.terminate()
            except Exception:
                pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except Exception:
                pass
        return True

    def _build_codex_cmd(self, rec: SessionRecord, *, prompt: str, run_mode: str) -> List[str]:
        sandbox_mode = _codex_sandbox_mode()
        approval_policy = _codex_approval_policy()
        base = ["codex", "exec", "--json", "--sandbox", sandbox_mode, "-c", f"approval_policy={approval_policy}"]

        # Если это git-репо (или путь внутри репо) — добавляем gitdir как writable dir.
        # Иначе включаем флаг, чтобы Codex не падал вне Git.
        git_dir = _detect_git_dir(Path(rec.path))
        if git_dir is None:
            base.append("--skip-git-repo-check")
        else:
            base += ["--add-dir", str(git_dir)]

        base += ["-C", rec.path]

        base += ["--model", rec.model]
        base += ["-c", f"model_reasoning_effort={rec.reasoning_effort}"]

        prompt_s = prompt or ""
        needs_end_of_opts = bool(prompt_s.lstrip().startswith("-"))
        if run_mode == "continue" and rec.thread_id:
            base += ["resume", rec.thread_id]
            if needs_end_of_opts:
                base.append("--")
            base.append(prompt_s)
        else:
            if needs_end_of_opts:
                base.append("--")
            base.append(prompt_s)
        return base

    def _build_claude_cmd(self, rec: SessionRecord, *, prompt: str, run_mode: str) -> List[str]:
        permission_mode = _claude_permission_mode()
        model = rec.model or _claude_model_default()
        base = [
            "claude",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--permission-mode",
            permission_mode,
            "--model",
            model,
        ]

        prompt_s = prompt or ""
        needs_end_of_opts = bool(prompt_s.lstrip().startswith("-"))
        if run_mode == "continue" and rec.thread_id:
            base += ["-r", rec.thread_id]
        if needs_end_of_opts:
            base.append("--")
        base.append(prompt_s)
        return base

    async def _spawn_process(self, cmd: List[str], *, cwd: Optional[str] = None) -> asyncio.subprocess.Process:
        kwargs: Dict[str, Any] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if cwd:
            kwargs["cwd"] = cwd
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        return await asyncio.create_subprocess_exec(*cmd, **kwargs)

    async def _read_stdout(
        self,
        *,
        rec: SessionRecord,
        process: asyncio.subprocess.Process,
        stream: TelegramStream,
        log_path: Path,
    ) -> None:
        assert process.stdout is not None
        log_f: Optional[Any] = None
        last_open_attempt_mono = 0.0

        def _try_open_log() -> Optional[Any]:
            nonlocal log_f, last_open_attempt_mono
            if log_f is not None:
                return log_f
            now_mono = time.monotonic()
            if (now_mono - last_open_attempt_mono) < 5.0:
                return None
            last_open_attempt_mono = now_mono
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_f = log_path.open("a", encoding="utf-8")
                return log_f
            except Exception as e:
                _log_error(f"Failed to open stdout log file: {log_path}", e)
                log_f = None
                return None

        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    _log_error("stdout.readline() failed.", e)
                    await asyncio.sleep(0.1)
                    continue

                if not line:
                    return

                decoded = line.decode("utf-8", errors="replace")

                f = _try_open_log()
                if f is not None:
                    try:
                        f.write(decoded)
                        f.flush()
                    except Exception as e:
                        _log_error(f"Failed to write stdout log file: {log_path}", e)
                        try:
                            f.close()
                        except Exception:
                            pass
                        log_f = None

                decoded_stripped = decoded.strip()
                if not decoded_stripped:
                    continue

                try:
                    # Пробуем JSONL.
                    obj: Optional[Dict[str, Any]] = None
                    try:
                        maybe = json.loads(decoded_stripped)
                        if isinstance(maybe, dict):
                            obj = maybe
                    except Exception:
                        obj = None

                    if not obj:
                        await stream.add_text(decoded)
                        continue

                    if rec.engine == ENGINE_CLAUDE:
                        await self._handle_claude_json_event(rec=rec, obj=obj, stream=stream)
                    else:
                        await self._handle_json_event(rec=rec, obj=obj, stream=stream)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    _log_error("stdout processing failed; continuing to read.", e)
                    continue
        finally:
            if log_f is not None:
                try:
                    log_f.close()
                except Exception:
                    pass

    async def _read_stderr(
        self,
        *,
        process: asyncio.subprocess.Process,
        log_path: Path,
        stderr_tail: Deque[str],
    ) -> None:
        assert process.stderr is not None
        log_f: Optional[Any] = None
        last_open_attempt_mono = 0.0

        def _try_open_log() -> Optional[Any]:
            nonlocal log_f, last_open_attempt_mono
            if log_f is not None:
                return log_f
            now_mono = time.monotonic()
            if (now_mono - last_open_attempt_mono) < 5.0:
                return None
            last_open_attempt_mono = now_mono
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_f = log_path.open("a", encoding="utf-8")
                return log_f
            except Exception as e:
                _log_error(f"Failed to open stderr log file: {log_path}", e)
                log_f = None
                return None

        try:
            while True:
                try:
                    line = await process.stderr.readline()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    _log_error("stderr.readline() failed.", e)
                    await asyncio.sleep(0.1)
                    continue

                if not line:
                    return

                decoded = line.decode("utf-8", errors="replace")

                f = _try_open_log()
                if f is not None:
                    try:
                        f.write(decoded)
                        f.flush()
                    except Exception as e:
                        _log_error(f"Failed to write stderr log file: {log_path}", e)
                        try:
                            f.close()
                        except Exception:
                            pass
                        log_f = None

                stderr_tail.append(decoded)
        finally:
            if log_f is not None:
                try:
                    log_f.close()
                except Exception:
                    pass

    async def _handle_json_event(self, *, rec: SessionRecord, obj: Dict[str, Any], stream: TelegramStream) -> None:
        event_type = _get_event_type(obj)

        # Сохраняем thread_id как можно раньше, но только если он явно помечен.
        if rec.thread_id is None:
            explicit_id = _extract_session_id_explicit(obj)
            if explicit_id:
                rec.thread_id = explicit_id
                rec.last_active = _utc_now_iso()
                await self.save_state()
                _log_line(f"thread_id_set session={rec.name} engine={rec.engine} thread_id={explicit_id}")

        if event_type in ("thread.started", "thread_started", "thread.start"):
            # На всякий случай: некоторые форматы могут не иметь session_id/thread_id в ожидаемых полях.
            session_id = _extract_session_id_explicit(obj) or _find_first_uuid(obj)
            if session_id and session_id != rec.thread_id:
                rec.thread_id = session_id
                rec.last_active = _utc_now_iso()
                await self.save_state()
                _log_line(f"thread_id_set session={rec.name} engine={rec.engine} thread_id={session_id}")
            return

        if event_type.startswith("item."):
            item = _extract_item(obj)
            if isinstance(item, dict):
                item_type = _extract_item_type(item)

                # Never surface hidden reasoning in chat logs.
                if item_type == "reasoning":
                    return

                if item_type == "command_execution":
                    cmd = item.get("command")
                    out = item.get("aggregated_output")
                    exit_code = item.get("exit_code")
                    status = item.get("status")

                    is_start = event_type.endswith("started") or status == "in_progress"
                    is_done = event_type.endswith("completed") or status in {"completed", "failed"}

                    cmd_s = cmd.strip() if isinstance(cmd, str) else ""
                    if cmd_s and (is_start or is_done):
                        last_cmd = rec.run.last_cmd if rec.run else None
                        if cmd_s != last_cmd:
                            await stream.add_text(f"\n$ {cmd_s}\n")
                            if rec.run:
                                rec.run.last_cmd = cmd_s

                    if is_done:
                        if isinstance(out, str) and out.strip():
                            out_s = out.rstrip("\n")
                            await stream.add_text(_truncate_text(out_s, 2000) + "\n")
                        if isinstance(exit_code, int):
                            await stream.add_text(f"(exit_code: {exit_code})\n")
                    return

                item_text = _extract_item_text(item)
                if item_text:
                    await stream.add_text(item_text)
                    return

        # streaming text
        if event_type == "text":
            delta = _extract_text_delta(obj)
            if delta:
                await stream.add_text(delta)
            return

        if event_type == "tool_use":
            cmd = _extract_tool_command(obj)
            if cmd:
                await stream.add_text(f"\n[tool_use]\n{cmd}\n")
            else:
                await stream.add_text("\n[tool_use]\n" + _truncate_text(json.dumps(obj, ensure_ascii=False, indent=2), 2000) + "\n")
            return

        if event_type == "tool_result":
            out = _extract_tool_output(obj)
            if out:
                await stream.add_text("\n[tool_result]\n" + _truncate_text(out, 2000) + "\n")
            else:
                await stream.add_text("\n[tool_result]\n" + _truncate_text(json.dumps(obj, ensure_ascii=False, indent=2), 2000) + "\n")
            return

        # File changes / diff (если присутствует)
        diff = _maybe_extract_diff(obj)
        if diff:
            await stream.add_text("\n[file_change]\n" + _truncate_text(diff, 2500) + "\n")
            return

        # Мягкий фоллбек: если видим текст — показываем, иначе игнорируем.
        delta = _extract_text_delta(obj)
        if delta:
            await stream.add_text(delta)
            return

    async def _handle_claude_json_event(self, *, rec: SessionRecord, obj: Dict[str, Any], stream: TelegramStream) -> None:
        session_id = _extract_claude_session_id(obj)
        if session_id and session_id != rec.thread_id:
            rec.thread_id = session_id
            rec.last_active = _utc_now_iso()
            await self.save_state()
            _log_line(f"thread_id_set session={rec.name} engine={rec.engine} thread_id={session_id}")

        delta = _extract_claude_text_delta(obj)
        if delta:
            await stream.add_text(delta)
            return


class PanelUI:
    def __init__(self, application: Application, manager: SessionManager) -> None:
        self.application = application
        self.manager = manager

    async def ensure_panel(self, chat_id: int) -> int:
        existing = self.manager.get_panel_message_id(chat_id)
        if existing:
            return existing

        msg = await self.application.bot.send_message(
            chat_id=chat_id,
            text="<b>Vibes</b>\n\nLoading…",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await self.manager.set_panel_message_id(chat_id, msg.message_id)
        return msg.message_id

    async def render_panel(
        self,
        chat_id: int,
        text_html: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> int:
        message_id = await self.ensure_panel(chat_id)
        return await self.render_to_message(
            chat_id=chat_id,
            message_id=message_id,
            text_html=text_html,
            reply_markup=reply_markup,
            update_state_on_replace=True,
        )

    async def render_to_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        text_html: str,
        reply_markup: Optional[InlineKeyboardMarkup],
        update_state_on_replace: bool,
    ) -> int:
        async def _send_new_panel(*, text: str, parse_mode: Optional[str]) -> int:
            kwargs: Dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
                "reply_markup": reply_markup,
            }
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
            msg = await self.application.bot.send_message(**kwargs)
            if update_state_on_replace:
                await self.manager.set_panel_message_id(chat_id, msg.message_id)
            return msg.message_id

        async def _edit_message(*, text: str, parse_mode: Optional[str]) -> None:
            kwargs: Dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "disable_web_page_preview": True,
                "reply_markup": reply_markup,
            }
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
            await self.application.bot.edit_message_text(**kwargs)

        try:
            await _edit_message(text=text_html, parse_mode=ParseMode.HTML)
            return message_id
        except RetryAfter as e:
            try:
                await asyncio.sleep(float(getattr(e, "retry_after", 2.0)))
                await _edit_message(text=text_html, parse_mode=ParseMode.HTML)
                return message_id
            except TelegramError:
                _log_error(f"Panel edit retry failed; sending new panel for chat_id={chat_id}, message_id={message_id}")
                return await _send_new_panel(text=text_html, parse_mode=ParseMode.HTML)
        except BadRequest as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                return message_id
            _log_error(f"Panel edit failed (BadRequest): {msg}", e)

            if "message is too long" in msg:
                trimmed_html = _telegram_safe_html_code_block(_strip_html_tags(text_html))
                try:
                    await _edit_message(text=trimmed_html, parse_mode=ParseMode.HTML)
                    return message_id
                except TelegramError as e2:
                    _log_error("Panel edit failed after trimming; falling back.", e2)

            if "can't parse entities" in msg or "can’t parse entities" in msg:
                plain = _truncate_text(_strip_html_tags(text_html), MAX_TELEGRAM_CHARS)
                try:
                    await _edit_message(text=plain, parse_mode=None)
                    return message_id
                except TelegramError as e2:
                    _log_error("Panel edit failed with plain-text fallback; falling back.", e2)

            # If we can no longer edit this message, send a replacement panel.
            if (
                "message can't be edited" in msg
                or "message to edit not found" in msg
                or "message_id_invalid" in msg
                or "chat not found" in msg
            ):
                return await _send_new_panel(text=text_html, parse_mode=ParseMode.HTML)

            # Last resort: try plain-text edit (no HTML). If that also fails, replace the panel.
            plain2 = _truncate_text(_strip_html_tags(text_html), MAX_TELEGRAM_CHARS)
            try:
                await _edit_message(text=plain2, parse_mode=None)
                return message_id
            except TelegramError as e2:
                _log_error("Panel edit failed (plain fallback); sending new panel.", e2)
                return await _send_new_panel(text=plain2, parse_mode=None)
        except TelegramError:
            _log_error(f"Panel edit failed (TelegramError); sending new panel for chat_id={chat_id}, message_id={message_id}")
            return await _send_new_panel(text=text_html, parse_mode=ParseMode.HTML)

    async def delete_message_best_effort(self, *, chat_id: int, message_id: int) -> None:
        try:
            await self.application.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass


def _h(text: str) -> str:
    return html.escape(text)


def _ui_get(chat_data: Dict[str, Any]) -> Dict[str, Any]:
    ui = chat_data.get("ui")
    if not isinstance(ui, dict):
        ui = {}
        chat_data["ui"] = ui
    return ui


def _ui_set(chat_data: Dict[str, Any], **fields: Any) -> None:
    ui = _ui_get(chat_data)
    ui.update(fields)


_UI_NAV_KEYS: Tuple[str, ...] = ("mode", "session", "new", "await_prompt", "return_to")


def _ui_nav_stack(chat_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    ui = _ui_get(chat_data)
    nav = ui.get("nav")
    if not isinstance(nav, list):
        nav = []
        ui["nav"] = nav
    return nav  # type: ignore[return-value]


def _ui_nav_snapshot(chat_data: Dict[str, Any]) -> Dict[str, Any]:
    ui = _ui_get(chat_data)
    snap: Dict[str, Any] = {}
    for k in _UI_NAV_KEYS:
        if k in ui:
            snap[k] = copy.deepcopy(ui.get(k))
    if "mode" not in snap:
        snap["mode"] = "sessions"
    return snap


def _ui_nav_push(chat_data: Dict[str, Any]) -> None:
    nav = _ui_nav_stack(chat_data)
    nav.append(_ui_nav_snapshot(chat_data))
    if len(nav) > 32:
        del nav[:16]


def _ui_nav_reset(chat_data: Dict[str, Any], *, to: Optional[Dict[str, Any]] = None) -> None:
    ui = _ui_get(chat_data)
    if to is None:
        ui["nav"] = []
        return
    if not isinstance(to, dict):
        ui["nav"] = []
        return
    ui["nav"] = [to]


def _ui_nav_restore(chat_data: Dict[str, Any], snap: Dict[str, Any]) -> None:
    ui = _ui_get(chat_data)
    for k in _UI_NAV_KEYS:
        ui.pop(k, None)
    for k, v in snap.items():
        if k in _UI_NAV_KEYS:
            ui[k] = v


def _ui_nav_pop(chat_data: Dict[str, Any]) -> bool:
    nav = _ui_nav_stack(chat_data)
    if not nav:
        return False
    current = _ui_nav_snapshot(chat_data)
    while nav:
        snap = nav.pop()
        if not isinstance(snap, dict):
            continue
        # Skip no-op snapshots (can happen when we navigate to the same screen repeatedly,
        # e.g. "session -> session" after a run finishes).
        if snap == current:
            continue
        _ui_nav_restore(chat_data, snap)
        return True
    return False


def _ui_nav_to(chat_data: Dict[str, Any], *, mode: str, push: bool = True, **fields: Any) -> None:
    if push:
        current = _ui_nav_snapshot(chat_data)
        desired: Dict[str, Any] = copy.deepcopy(current)
        desired["mode"] = mode
        for k, v in fields.items():
            if k in _UI_NAV_KEYS:
                desired[k] = copy.deepcopy(v)
        if desired != current:
            _ui_nav_push(chat_data)
    _ui_set(chat_data, mode=mode, **fields)


def _ui_sanitize(manager: "SessionManager", chat_data: Dict[str, Any]) -> None:
    ui = _ui_get(chat_data)
    mode = ui.get("mode") if isinstance(ui.get("mode"), str) else "sessions"
    session_name = ui.get("session") if isinstance(ui.get("session"), str) else None
    if mode in {"session", "logs", "model", "model_custom", "confirm_delete", "confirm_stop", "await_prompt", "info"}:
        if not session_name or session_name not in manager.sessions:
            _ui_set(chat_data, mode="sessions")


def _build_running_header_plain(rec: SessionRecord, *, note: Optional[str] = None) -> str:
    model = rec.model
    reasoning_effort = rec.reasoning_effort
    lines = [
        f"Session: {rec.name}",
        f"Path: {rec.path}",
        f"Engine: {rec.engine}",
        f"Model: {model}",
    ]
    if rec.engine == ENGINE_CODEX:
        lines.append(f"Reasoning effort: {reasoning_effort}")
    lines.append(f"Status: {rec.status}")
    if note:
        lines.append(note)
    return "\n".join(lines)


def _build_running_header_plain_len(rec: SessionRecord, *, note: Optional[str] = None) -> int:
    return len(_build_running_header_plain(rec, note=note))


def _build_running_header_html(rec: SessionRecord, *, note: Optional[str] = None) -> str:
    model = rec.model
    reasoning_effort = rec.reasoning_effort
    note_line = f"\n<i>{_h(note)}</i>" if note else ""
    reasoning_line = (
        f"\n<b>Reasoning effort:</b> <code>{_h(reasoning_effort)}</code>"
        if rec.engine == ENGINE_CODEX
        else ""
    )
    return (
        f"<b>Session:</b> <code>{_h(rec.name)}</code>\n"
        f"<b>Path:</b> <code>{_h(rec.path)}</code>\n"
        f"<b>Engine:</b> <code>{_h(rec.engine)}</code>\n"
        f"<b>Model:</b> <code>{_h(model)}</code>\n"
        f"{reasoning_line}\n"
        f"<b>Status:</b> {_h(rec.status)}"
        f"{note_line}"
    )


def _parse_tokens(message_text: str) -> List[str]:
    try:
        tokens = shlex.split(message_text, posix=True)
    except ValueError:
        tokens = message_text.split()
    if tokens:
        # /cmd@botname -> /cmd
        tokens[0] = tokens[0].split("@", 1)[0]
    return tokens


_STOP_CONFIRM_QUESTION = "Are you sure you want to stop this run?"


def _detach_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(LABEL_BACK, callback_data=_cb("detach"))]])


def _stop_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes, stop", callback_data=_cb("stop_yes")),
                InlineKeyboardButton("❌ No", callback_data=_cb("stop_no")),
            ]
        ]
    )


def _status_emoji(rec: SessionRecord) -> str:
    if rec.status == "running":
        return "🟢"
    if rec.last_result == "success" and rec.status == "idle":
        return "✅"
    if rec.status == "stopped" or rec.last_result == "stopped":
        return "⏹"
    if rec.status == "error" or rec.last_result == "error":
        return "❌"
    if rec.last_result == "never":
        return "🆕"
    return "⚪️"


def _is_running(rec: SessionRecord) -> bool:
    return bool(rec.run and rec.run.process.returncode is None and rec.status == "running")


async def _show_stop_confirmation_in_stream(rec: SessionRecord) -> None:
    if not rec.run:
        return
    rec.run.confirm_stop = True
    rec.run.header_note = _STOP_CONFIRM_QUESTION
    await rec.run.stream.set_header(
        header_html=_build_running_header_html(rec, note=_STOP_CONFIRM_QUESTION),
        header_plain_len=_build_running_header_plain_len(rec, note=_STOP_CONFIRM_QUESTION),
    )
    await rec.run.stream.set_reply_markup(_stop_confirm_keyboard())


async def _restore_run_stream_ui(rec: SessionRecord) -> None:
    if not rec.run:
        return
    rec.run.confirm_stop = False
    rec.run.header_note = None
    await rec.run.stream.set_header(
        header_html=_build_running_header_html(rec),
        header_plain_len=_build_running_header_plain_len(rec),
    )
    await rec.run.stream.set_reply_markup(_detach_keyboard())


def _shorten_path(path: str, *, max_len: int = 34) -> str:
    p = path.strip()
    if len(p) <= max_len:
        return p
    parts = p.replace("\\", "/").split("/")
    tail = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    if len(tail) + 2 >= max_len:
        return "…" + tail[-(max_len - 1) :]
    return f"…/{tail}"


def _last_log_summary(rec: SessionRecord) -> str:
    if rec.last_result == "never" and not rec.thread_id and not rec.last_stdout_log and not rec.last_stderr_log:
        return "Never run yet."

    last_msg = _extract_last_agent_message_from_stdout_log(rec.last_stdout_log, max_chars=1200)
    if last_msg:
        for ln in (ln.strip() for ln in last_msg.splitlines()):
            if ln:
                return _truncate_text(ln, 320)

    out = _preview_from_stdout_log(rec.last_stdout_log, max_chars=1200)
    if out:
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if lines:
            return _truncate_text(lines[-1], 320)

    err = _preview_from_stderr_log(rec.last_stderr_log, max_chars=800)
    if err:
        lines2 = [ln.strip() for ln in err.splitlines() if ln.strip()]
        if lines2:
            return _truncate_text(lines2[-1], 320)

    if rec.last_result == "success":
        return "Success."
    if rec.last_result == "stopped":
        return "Stopped."
    return "Error."


def _format_last_active(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        dt_obj = dt.datetime.fromisoformat(value)
    except Exception:
        return value
    if dt_obj.tzinfo is not None:
        dt_obj = dt_obj.astimezone().replace(tzinfo=None)
    return dt_obj.strftime("%Y-%m-%d %H:%M")


def _home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📂", callback_data=_cb("sessions")),
                InlineKeyboardButton("➕", callback_data=_cb("new")),
            ],
        ]
    )


def _render_home(manager: SessionManager, *, notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    admin_note = ""
    if manager._admin_id is None:
        admin_note = "\n\n<i>Warning:</i> this bot is running without <code>--admin</code> — anyone who finds it can control it."

    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    text_html = (
        f"{notice_html}"
        "<b>Vibes</b> is a lightweight session manager for Codex CLI.\n\n"
        "It keeps this chat clean by editing a single panel message and deleting your messages.\n\n"
        "Use the buttons below to manage sessions, pick working directories, and run prompts."
        f"{admin_note}"
    )
    return text_html, _home_keyboard()


def _render_sessions_list(
    manager: SessionManager,
    *,
    chat_data: Dict[str, Any],
    notice: Optional[str] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    names = sorted(manager.sessions.keys())
    _ui_set(chat_data, sess_list=names)

    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    if not names:
        text_html = (
            f"{notice_html}"
            "<b>Vibes</b> is a lightweight session manager for Codex CLI.\n\n"
            "Choose or create session:"
        )

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕", callback_data=_cb("new"))],
                [InlineKeyboardButton("🔄", callback_data=_cb("restart"))],
            ]
        )
        return text_html, kb

    rows: List[List[InlineKeyboardButton]] = []
    for i, name in enumerate(names):
        rec = manager.sessions[name]
        label = f"{_status_emoji(rec)} {name}"
        rows.append([InlineKeyboardButton(label, callback_data=_cb("sess", str(i)))])

    rows.append([InlineKeyboardButton("➕", callback_data=_cb("new"))])
    rows.append([InlineKeyboardButton("🔄", callback_data=_cb("restart"))])
    text_html = (
        f"{notice_html}"
        "<b>Vibes</b> is a lightweight session manager for Codex CLI and Claude Code.\n\n"
        "Choose or create session:"
    )
    return text_html, InlineKeyboardMarkup(rows)


def _render_new_name(manager: SessionManager, *, chat_data: Dict[str, Any], notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    auto_name = manager.next_auto_session_name()
    _ui_set(chat_data, auto_name=auto_name)
    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    text_html = (
        f"{notice_html}"
        "<b>Step 1/3 — Name</b>\n\n"
        "Send a session name: <code>a-zA-Z0-9._-</code>.\n"
        "Or tap the suggested name below."
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"{auto_name}", callback_data=_cb("new_auto"))],
            [InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))],
        ]
    )
    return text_html, kb


def _render_new_engine(*, chat_data: Dict[str, Any], notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    ui = _ui_get(chat_data)
    draft = ui.get("new")
    name = draft.get("name") if isinstance(draft, dict) else None
    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    name_line = f"Session: <code>{_h(name)}</code>\n\n" if isinstance(name, str) and name else ""
    text_html = (
        f"{notice_html}"
        "<b>Step 2/3 — Engine</b>\n\n"
        f"{name_line}"
        "Pick the CLI engine for this session."
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Codex CLI", callback_data=_cb("engine", ENGINE_CODEX))],
            [InlineKeyboardButton("Claude Code", callback_data=_cb("engine", ENGINE_CLAUDE))],
            [InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))],
        ]
    )
    return text_html, kb


def _render_new_path(
    manager: SessionManager,
    *,
    chat_data: Dict[str, Any],
    notice: Optional[str] = None,
    notice_code: Optional[str] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    ui = _ui_get(chat_data)
    draft = ui.get("new")
    name = draft.get("name") if isinstance(draft, dict) else None
    engine = draft.get("engine") if isinstance(draft, dict) else None
    path_mode = draft.get("path_mode") if isinstance(draft, dict) else None

    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    notice_code_html = f"<b>Путь:</b> <code>{_h(notice_code)}</code>\n\n" if notice_code else ""
    docs_root = _default_projects_root()
    docs_label = _pretty_path(docs_root)
    engine_line = f"Engine: <code>{_h(engine)}</code>\n\n" if isinstance(engine, str) and engine else ""

    if path_mode not in {"docs", "full"}:
        text_html = (
            f"{notice_html}"
            "<b>Step 3/3 — Где работать?</b>\n\n"
            f"{engine_line}"
            "Выбери вариант ниже.\n\n"
            f"<b>Documents:</b> <code>{_h(docs_label)}</code>\n"
            "• <i>Создать в Documents</i> — ты пишешь только имя папки.\n"
            "• <i>Полный путь</i> — ты указываешь директорию целиком."
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📁 Создать в Documents", callback_data=_cb("path_mode", "docs"))],
                [InlineKeyboardButton("🧭 Указать полный путь", callback_data=_cb("path_mode", "full"))],
                [InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))],
            ]
        )
        return text_html, kb

    if path_mode == "docs":
        text_html = (
            f"{notice_html}"
            "<b>Step 3/3 — Имя папки</b>\n\n"
            f"{engine_line}"
            f"Будем работать в: <code>{_h(docs_label)}</code>\n\n"
            "Напиши <b>название папки</b> (например: <code>my-project</code>).\n"
            "Папка будет создана в Documents."
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("← Выбор места", callback_data=_cb("path_mode", "reset"))],
                [InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))],
            ]
        )
        return text_html, kb

    # path_mode == "full"
    text_html = (
        f"{notice_html}"
        "<b>Step 3/3 — Полный путь</b>\n\n"
        f"{notice_code_html}"
        f"{engine_line}"
        "Укажи полный путь к директории (или выбери пресет ниже).\n\n"
        "<i>Подсказка: можно использовать <code>~/</code> как домашнюю папку.</i>\n"
        "<i>Пример: <code>~/projects/my-app</code></i>\n\n"
        "<b>Нажми на путь, чтобы скопировать.</b>"
    )

    rows: List[List[InlineKeyboardButton]] = []
    for i, p in enumerate(manager.path_presets):
        rows.append([InlineKeyboardButton(f"📁 {_shorten_path(p)}", callback_data=_cb("path_pick", str(i)))])
    rows.append([InlineKeyboardButton("⚙️ Пресеты", callback_data=_cb("paths"))])
    rows.append([InlineKeyboardButton("← Выбор места", callback_data=_cb("path_mode", "reset"))])
    rows.append([InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))])
    return text_html, InlineKeyboardMarkup(rows)


def _render_paths(
    manager: SessionManager,
    *,
    chat_data: Dict[str, Any],
    notice: Optional[str] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    lines = ["<b>Paths presets</b>", "", "These appear as quick buttons in the New session wizard.", ""]
    if manager.path_presets:
        for i, p in enumerate(manager.path_presets, start=1):
            lines.append(f"{i}. <code>{_h(p)}</code>")
    else:
        lines.append("<i>No presets yet.</i>")

    rows: List[List[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton("➕", callback_data=_cb("paths_add"))])
    del_buttons: List[InlineKeyboardButton] = []
    for i, p in enumerate(manager.path_presets):
        label = f"🗑 #{i+1}"
        del_buttons.append(InlineKeyboardButton(label, callback_data=_cb("path_del", str(i))))
    for i in range(0, len(del_buttons), 3):
        rows.append(del_buttons[i : i + 3])
    rows.append([InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))])
    text_html = notice_html + "\n".join(lines)
    return text_html, InlineKeyboardMarkup(rows)


def _render_paths_add(*, notice: Optional[str] = None, notice_code: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    notice_code_html = f"<b>Путь:</b> <code>{_h(notice_code)}</code>\n\n" if notice_code else ""
    text_html = (
        f"{notice_html}"
        "<b>Add path preset</b>\n\n"
        f"{notice_code_html}"
        "Send a directory path. I will validate it and add it to presets.\n\n"
        "<i>Tip: you can use <code>~/</code> as your home directory.</i>\n"
        "<i>For example: <code>~/projects/my-app</code></i>\n\n"
        "<b>Click on path to copy!</b>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))]])
    return text_html, kb


def _render_confirm_delete(rec: SessionRecord, *, notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    text_html = (
        f"{notice_html}"
        "<b>Delete session?</b>\n\n"
        f"Session: <code>{_h(rec.name)}</code>\n"
        f"Path: <code>{_h(rec.path)}</code>\n\n"
        "<b>This will delete only bot artifacts</b> (state + logs).\n"
        "<b>Your project directory will NOT be deleted.</b>"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅", callback_data=_cb("delete_yes")),
                InlineKeyboardButton("❌", callback_data=_cb("delete_no")),
            ]
        ]
    )
    return text_html, kb


def _render_confirm_mkdir(*, chat_data: Dict[str, Any], notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    ui = _ui_get(chat_data)
    mkdir = ui.get("mkdir")
    path = mkdir.get("path") if isinstance(mkdir, dict) else None

    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    if not isinstance(path, str) or not path:
        text_html = f"{notice_html}<b>Create directory?</b>\n\n<i>No pending directory.</i>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))]])
        return text_html, kb

    text_html = (
        f"{notice_html}"
        "<b>Create directory?</b>\n\n"
        f"<code>{_h(path)}</code>\n\n"
        "This folder doesn’t exist. Create it (including parents)?"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅", callback_data=_cb("mkdir_yes")),
                InlineKeyboardButton("❌", callback_data=_cb("mkdir_no")),
            ]
        ]
    )
    return text_html, kb


def _render_confirm_stop(session_name: str, *, notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    text_html = (
        f"{notice_html}"
        "<b>Stop run?</b>\n\n"
        f"Session: <code>{_h(session_name)}</code>\n\n"
        "This will interrupt the current run."
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅", callback_data=_cb("stop_yes")),
                InlineKeyboardButton("❌", callback_data=_cb("stop_no")),
            ]
        ]
    )
    return text_html, kb


def _render_session_compact_info(rec: SessionRecord) -> str:
    model = rec.model
    reasoning_effort = rec.reasoning_effort
    if rec.engine == ENGINE_CLAUDE:
        return f"<code>{_h(rec.engine)}</code> <code>{_h(model)}</code>\n<code>{_h(rec.path)}</code>"
    return f"<code>{_h(rec.engine)}</code> <code>{_h(model)}</code> <code>{_h(reasoning_effort)}</code>\n<code>{_h(rec.path)}</code>"


def _render_model(rec: SessionRecord, *, notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    current = rec.model
    reasoning_effort = rec.reasoning_effort
    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    engine_line = f"Engine: <code>{_h(rec.engine)}</code>"
    lines = [
        f"{notice_html}<b>Run settings</b>",
        "",
        _render_session_compact_info(rec),
        "",
        engine_line,
        f"Model: <code>{_h(current)}</code>",
    ]

    if rec.engine == ENGINE_CLAUDE:
        lines += ["", "Claude uses model ids; use 📝 to set a custom model."]
        rows: List[List[InlineKeyboardButton]] = [
            [InlineKeyboardButton("📝", callback_data=_cb("model_custom"))],
            [InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))],
        ]
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    lines += [
        f"Reasoning effort: <code>{_h(reasoning_effort)}</code>",
        "",
        "Pick overrides below.",
    ]
    rows: List[List[InlineKeyboardButton]] = []

    def _mark(label: str, selected: bool) -> str:
        return f"✅ {label}" if selected else label

    buttons = [
        InlineKeyboardButton(_mark(m, m == current), callback_data=_cb("model_pick", str(i)))
        for i, m in enumerate(MODEL_PRESETS)
    ]
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    rows.append(
        [
            InlineKeyboardButton(
                _mark("📝", current not in MODEL_PRESETS),
                callback_data=_cb("model_custom"),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(_mark("low", reasoning_effort == "low"), callback_data=_cb("reasoning_pick", "low")),
            InlineKeyboardButton(
                _mark("medium", reasoning_effort == "medium"), callback_data=_cb("reasoning_pick", "medium")
            ),
            InlineKeyboardButton(_mark("high", reasoning_effort == "high"), callback_data=_cb("reasoning_pick", "high")),
            InlineKeyboardButton(
                _mark("xhigh", reasoning_effort == "xhigh"), callback_data=_cb("reasoning_pick", "xhigh")
            ),
        ]
    )
    rows.append([InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _render_model_custom(rec: SessionRecord, *, notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    if rec.engine == ENGINE_CLAUDE:
        example = _claude_model_default()
    else:
        example = MODEL_PRESETS[0] if MODEL_PRESETS else "o3"
    text_html = (
        f"{notice_html}"
        "<b>Custom model</b>\n\n"
        f"{_render_session_compact_info(rec)}\n\n"
        f"Send a model id (e.g. <code>{_h(example)}</code>) or tap Back."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))]])
    return text_html, kb


def _render_await_prompt(
    session_name: str,
    *,
    run_mode: str,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    engine: Optional[str] = None,
    path: Optional[str] = None,
    notice: Optional[str] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    mode_label = "continue (resume)" if run_mode == "continue" else "new prompt"
    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    engine_label = engine or ENGINE_CODEX
    model_label = model or (_claude_model_default() if engine_label == ENGINE_CLAUDE else DEFAULT_MODEL)
    reasoning_label = reasoning_effort or DEFAULT_REASONING_EFFORT
    path_label = path or ""
    path_line = f"<code>{_h(path_label)}</code>\n" if path_label else ""
    engine_line = f"<code>{_h(engine_label)}</code>\n"
    reasoning_line = (
        f"<code>{_h(reasoning_label)}</code>\n" if engine_label == ENGINE_CODEX else ""
    )
    text_html = (
        f"{notice_html}"
        f"<b>Session:</b> <code>{_h(session_name)}</code>\n"
        f"{engine_line}"
        f"<code>{_h(model_label)}</code>\n"
        f"{reasoning_line}"
        f"{path_line}\n"
        "Напиши промт сообщением.\n\n"
        f"<i>Режим:</i> {_h(mode_label)}"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⚙️", callback_data=_cb("model")),
                InlineKeyboardButton("ℹ️", callback_data=_cb("info")),
            ],
            [InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))],
        ]
    )
    return text_html, kb


def _render_info(rec: SessionRecord, *, notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    duration = _format_duration(rec.last_run_duration_s or 0) if rec.last_run_duration_s else "—"
    reasoning_line = (
        f"Reasoning effort: <code>{_h(rec.reasoning_effort)}</code>\n" if rec.engine == ENGINE_CODEX else ""
    )
    text_html = (
        f"{notice_html}"
        "<b>Session info</b>\n\n"
        f"Name: <code>{_h(rec.name)}</code>\n"
        f"Engine: <code>{_h(rec.engine)}</code>\n"
        f"Model: <code>{_h(rec.model)}</code>\n"
        f"{reasoning_line}"
        f"Status: <code>{_h(rec.status)}</code>\n"
        f"Last result: <code>{_h(rec.last_result)}</code>\n"
        f"Last run: <code>{_h(duration)}</code>\n"
        f"Path: <code>{_h(rec.path)}</code>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))]])
    return text_html, kb


def _render_session_view(
    manager: SessionManager,
    *,
    session_name: str,
    notice: Optional[str] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    rec = manager.sessions.get(session_name)
    if not rec:
        return _render_sessions_list(manager, chat_data={}, notice=f"Unknown session: {session_name}")

    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""

    model = rec.model
    reasoning_effort = rec.reasoning_effort
    compact_info = _render_session_compact_info(rec)

    if _is_running(rec) and rec.run:
        elapsed_s = int(time.monotonic() - rec.run.started_mono)
        text_html = (
            f"{notice_html}"
            f"<b>Running…</b>\n\n"
            f"{_render_session_compact_info(rec)}\n\n"
            "Вывод идёт отдельным сообщением ниже.\n\n"
            f"<code>---- Working {_h(_format_duration(elapsed_s))} ----</code>"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⬅️", callback_data=_cb("back_sessions")),
                    InlineKeyboardButton("⛔", callback_data=_cb("interrupt")),
                ],
                [InlineKeyboardButton("ℹ️", callback_data=_cb("info"))],
            ]
        )
        return text_html, kb

    never_run = (
        rec.last_result == "never"
        and not rec.thread_id
        and not rec.last_stdout_log
        and not rec.last_stderr_log
        and rec.last_run_duration_s is None
    )

    if never_run:
        text_html = f"{notice_html}{compact_info}\n\n<i>Send a prompt to start.</i>"
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⚙️", callback_data=_cb("model")), InlineKeyboardButton("ℹ️", callback_data=_cb("info"))],
                [InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back")), InlineKeyboardButton("🗑", callback_data=_cb("delete"))],
            ]
        )
        return text_html, kb

    # Finished (success/error/stopped)
    stdout_plain = _preview_from_stdout_log(rec.last_stdout_log, max_chars=100000).strip()
    stderr_plain = _preview_from_stderr_log(rec.last_stderr_log, max_chars=100000).strip()
    log_plain = stdout_plain or stderr_plain or "(empty)"

    status_kind = "worked"
    if rec.last_result == "stopped" or rec.status == "stopped":
        status_kind = "stopped"
    elif rec.last_result == "error" or rec.status == "error":
        status_kind = "failed"

    duration_s = rec.last_run_duration_s if isinstance(rec.last_run_duration_s, int) else 0
    duration_label = _format_duration(duration_s)
    status_line = {
        "worked": f"<code>---- Worked for {_h(duration_label)} ----</code>",
        "stopped": f"<code>---- Stopped after {_h(duration_label)} ----</code>",
        "failed": f"<code>---- Failed after {_h(duration_label)} ----</code>",
    }[status_kind]

    result_plain = _extract_last_agent_message_from_stdout_log(rec.last_stdout_log, max_chars=100000).strip()
    result_plain = result_plain or ""

    log_max = 2600
    result_max = 1400
    for _ in range(10):
        log_tail = _tail_text(log_plain, log_max)
        result_view = result_plain
        if result_view and len(result_view) > result_max:
            result_view = _truncate_text(result_view, result_max)

        if "\n" in result_view:
            result_html = f"<pre><code>{_h(result_view)}</code></pre>" if result_view else ""
        else:
            result_html = _h(result_view) if result_view else ""

        parts = [
            notice_html.rstrip(),
            f"<pre><code>{_h(log_tail)}</code></pre>",
            compact_info,
            status_line,
        ]
        if result_html:
            parts.append(result_html)
        parts.append("Send a prompt to continue.")

        text_html = "\n\n".join([p for p in parts if p])
        if len(text_html) <= MAX_TELEGRAM_CHARS:
            break
        if log_max > 900:
            log_max = max(900, int(log_max * 0.8))
            continue
        if result_max > 300:
            result_max = max(300, int(result_max * 0.8))
            continue
        break

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🆕", callback_data=_cb("clear")), InlineKeyboardButton("⚙️", callback_data=_cb("model"))],
            [InlineKeyboardButton("ℹ️", callback_data=_cb("info"))],
            [InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back")), InlineKeyboardButton("🗑", callback_data=_cb("delete"))],
        ]
    )
    return text_html, kb


def _render_logs_view(
    manager: SessionManager,
    *,
    session_name: str,
    notice: Optional[str] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    rec = manager.sessions.get(session_name)
    if not rec:
        return _render_sessions_list(manager, chat_data={}, notice=f"Unknown session: {session_name}")

    last_msg = _extract_last_agent_message_from_stdout_log(rec.last_stdout_log, max_chars=3200)
    if not last_msg:
        last_msg = _preview_from_stdout_log(rec.last_stdout_log, max_chars=3200)
    if not last_msg:
        last_msg = "(empty)"

    notice_html = f"<i>{_h(notice)}</i>\n\n" if notice else ""
    text_html = (
        f"{notice_html}"
        f"<b>Log</b> <code>{_h(rec.name)}</code>\n\n"
        f"{_render_session_compact_info(rec)}\n\n"
        f"<pre><code>{_h(last_msg)}</code></pre>"
    )

    kb = InlineKeyboardMarkup([[InlineKeyboardButton(LABEL_BACK, callback_data=_cb("back"))]])
    return text_html, kb


def _render_current(manager: SessionManager, *, chat_data: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    ui = _ui_get(chat_data)
    mode = ui.get("mode") if isinstance(ui.get("mode"), str) else "sessions"
    notice = ui.pop("notice", None) if isinstance(ui.get("notice"), str) else None
    notice_code = ui.pop("notice_code", None) if isinstance(ui.get("notice_code"), str) else None

    if mode == "home":
        return _render_home(manager, notice=notice)
    if mode == "sessions":
        return _render_sessions_list(manager, chat_data=chat_data, notice=notice)
    if mode == "new_name":
        return _render_new_name(manager, chat_data=chat_data, notice=notice)
    if mode == "new_engine":
        return _render_new_engine(chat_data=chat_data, notice=notice)
    if mode == "new_path":
        return _render_new_path(manager, chat_data=chat_data, notice=notice, notice_code=notice_code)
    if mode == "info":
        session_name = ui.get("session")
        rec = manager.sessions.get(session_name) if isinstance(session_name, str) else None
        if rec:
            return _render_info(rec, notice=notice)
        return _render_sessions_list(manager, chat_data=chat_data, notice="No session selected.")
    if mode == "paths":
        return _render_paths(manager, chat_data=chat_data, notice=notice)
    if mode == "paths_add":
        return _render_paths_add(notice=notice, notice_code=notice_code)
    if mode == "await_prompt":
        session_name = ui.get("session")
        await_prompt = ui.get("await_prompt")
        run_mode = await_prompt.get("run_mode") if isinstance(await_prompt, dict) else "new"
        if isinstance(session_name, str) and session_name:
            rec = manager.sessions.get(session_name)
            return _render_await_prompt(
                session_name,
                run_mode=run_mode,
                model=(rec.model if rec else None),
                reasoning_effort=(rec.reasoning_effort if rec else None),
                engine=(rec.engine if rec else None),
                path=(rec.path if rec else None),
                notice=notice,
            )
        return _render_sessions_list(manager, chat_data=chat_data, notice="No session selected.")
    if mode == "confirm_delete":
        session_name2 = ui.get("session")
        rec = manager.sessions.get(session_name2) if isinstance(session_name2, str) else None
        if rec:
            return _render_confirm_delete(rec, notice=notice)
        return _render_sessions_list(manager, chat_data=chat_data, notice="Unknown session.")
    if mode == "confirm_mkdir":
        return _render_confirm_mkdir(chat_data=chat_data, notice=notice)
    if mode == "confirm_stop":
        session_name_stop = ui.get("session")
        if isinstance(session_name_stop, str) and session_name_stop in manager.sessions:
            return _render_confirm_stop(session_name_stop, notice=notice)
        return _render_sessions_list(manager, chat_data=chat_data, notice="No session selected.")
    if mode == "model":
        session_name3 = ui.get("session")
        rec2 = manager.sessions.get(session_name3) if isinstance(session_name3, str) else None
        if rec2:
            return _render_model(rec2, notice=notice)
        return _render_sessions_list(manager, chat_data=chat_data, notice="Unknown session.")
    if mode == "model_custom":
        session_name_custom = ui.get("session")
        rec_custom = manager.sessions.get(session_name_custom) if isinstance(session_name_custom, str) else None
        if rec_custom:
            return _render_model_custom(rec_custom, notice=notice)
        return _render_sessions_list(manager, chat_data=chat_data, notice="No session selected.")
    if mode == "logs":
        session_name4 = ui.get("session")
        if isinstance(session_name4, str) and session_name4:
            return _render_logs_view(manager, session_name=session_name4, notice=notice)
        return _render_sessions_list(manager, chat_data=chat_data, notice="No session selected.")
    if mode == "session":
        session_name5 = ui.get("session")
        if isinstance(session_name5, str) and session_name5:
            return _render_session_view(manager, session_name=session_name5, notice=notice)
        return _render_sessions_list(manager, chat_data=chat_data, notice=notice)

    return _render_sessions_list(manager, chat_data=chat_data, notice=notice)


async def _delete_user_message_best_effort(update: Update, *, authorized: bool) -> None:
    if not authorized:
        return
    msg = update.message
    if not msg:
        return
    chat = update.effective_chat
    chat_type = getattr(chat, "type", None) if chat is not None else None
    if chat_type == "private":
        pass
    elif chat_type in {"group", "supergroup"}:
        # Safety: avoid deleting messages in groups by default.
        if not _env_flag("VIBES_DELETE_MESSAGES_IN_GROUPS"):
            return
    else:
        return
    try:
        await msg.delete()
    except TelegramError:
        pass
    except Exception as e:
        _log_error("Failed to delete user message.", e)


async def _clear_input_prompt(panel: PanelUI, *, chat_id: int, chat_data: Dict[str, Any]) -> None:
    ui = _ui_get(chat_data)
    prompt = ui.pop("input_prompt", None)
    if not isinstance(prompt, dict):
        return
    try:
        msg_id = int(prompt.get("message_id"))
    except Exception:
        return
    if msg_id > 0:
        await panel.delete_message_best_effort(chat_id=chat_id, message_id=msg_id)


async def _sync_input_prompt(
    panel: PanelUI,
    *,
    chat_id: int,
    chat_data: Dict[str, Any],
) -> None:
    await _clear_input_prompt(panel, chat_id=chat_id, chat_data=chat_data)


async def _render_and_sync(
    manager: SessionManager,
    panel: PanelUI,
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    # When a run is "attached", its TelegramStream continuously edits the same panel message.
    # Any UI render (logs/sessions/etc) must first pause attached runs, otherwise a finishing run
    # can overwrite the UI and remove its buttons.
    panel_message_id = manager.get_panel_message_id(chat_id)
    if not panel_message_id:
        panel_message_id = await panel.ensure_panel(chat_id)

    ui = _ui_get(context.chat_data)
    mode = ui.get("mode") if isinstance(ui.get("mode"), str) else "sessions"
    session_name = ui.get("session") if isinstance(ui.get("session"), str) else None

    # When viewing a running session, the session screen itself is the live stream.
    # Do not pause it; re-attach and let TelegramStream drive the panel message.
    if mode == "session" and isinstance(session_name, str) and session_name in manager.sessions:
        rec = manager.sessions.get(session_name)
        if rec and _is_running(rec) and rec.run:
            try:
                if rec.run.stream.get_chat_id() == chat_id and rec.run.stream.get_message_id() == panel_message_id:
                    try:
                        await manager.pause_other_attached_runs(
                            chat_id=chat_id,
                            message_id=panel_message_id,
                            except_session=rec.name,
                        )
                    except Exception as e:
                        _log_error("pause_other_attached_runs failed (_render_and_sync attach).", e)

                    manager.register_run_message(chat_id=chat_id, message_id=panel_message_id, session_name=rec.name)
                    rec.run.paused = False

                    def _working_footer_html() -> str:
                        elapsed_s = int(time.monotonic() - rec.run.started_mono)
                        return f"<code>---- Working {_h(_format_duration(elapsed_s))} ----</code>"

                    await rec.run.stream.set_footer(
                        footer_provider=_working_footer_html,
                        footer_plain_len=len("---- Working 0m 0s ----"),
                        wrap_log_in_pre=True,
                    )
                    await rec.run.stream.set_reply_markup(
                        InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton("⬅️", callback_data=_cb("back_sessions")),
                                    InlineKeyboardButton("⛔", callback_data=_cb("interrupt")),
                                ]
                            ]
                        )
                    )

                    # Ensure we don't show stale notices later (the running panel doesn't render notices).
                    if isinstance(ui.get("notice"), str):
                        ui.pop("notice", None)

                    await rec.run.stream.resume()
                    await _sync_input_prompt(panel, chat_id=chat_id, chat_data=context.chat_data)
                    return
            except Exception:
                # Fall back to normal UI rendering.
                pass

    try:
        await manager.pause_other_attached_runs(chat_id=chat_id, message_id=panel_message_id)
    except Exception as e:
        _log_error("pause_other_attached_runs failed (_render_and_sync).", e)

    text_html, reply_markup = _render_current(manager, chat_data=context.chat_data)
    await panel.render_to_message(
        chat_id=chat_id,
        message_id=panel_message_id,
        text_html=text_html,
        reply_markup=reply_markup,
        update_state_on_replace=True,
    )
    await _sync_input_prompt(panel, chat_id=chat_id, chat_data=context.chat_data)


async def _deny_and_render(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    manager: SessionManager = context.application.bot_data["manager"]
    panel: PanelUI = context.application.bot_data["panel"]
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return
    _ui_set(context.chat_data, mode="home", notice="Access denied.")
    await panel.render_panel(chat_id, *_render_home(manager, notice="Access denied."))
    await _sync_input_prompt(panel, chat_id=chat_id, chat_data=context.chat_data)


async def _ensure_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    manager: SessionManager = context.application.bot_data["manager"]
    if await manager.ensure_owner(update):
        return True
    user = update.effective_user
    chat = update.effective_chat
    _log_line(
        f"access_denied user_id={getattr(user, 'id', None)} chat_id={getattr(chat, 'id', None)}"
    )
    await _deny_and_render(update, context)
    return False


@dataclasses.dataclass(frozen=True)
class _HandlerEnv:
    manager: SessionManager
    panel: PanelUI
    chat_id: int


async def _get_handler_env(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    delete_user_message: bool = True,
) -> Optional[_HandlerEnv]:
    manager: SessionManager = context.application.bot_data["manager"]
    panel: PanelUI = context.application.bot_data["panel"]
    if not await _ensure_authorized(update, context):
        return None
    if delete_user_message:
        await _delete_user_message_best_effort(update, authorized=True)
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return None
    return _HandlerEnv(manager=manager, panel=panel, chat_id=chat_id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    env = await _get_handler_env(update, context, delete_user_message=False)
    if not env:
        return
    _ui_nav_reset(context.chat_data)
    _ui_set(context.chat_data, mode="sessions")
    # /start is commonly used as a "reset". If the user cleared the chat history, the stored
    # panel message id may no longer be visible. Force a fresh panel message when safe.
    old_panel_id = env.manager.get_panel_message_id(env.chat_id)
    has_running_in_chat = False
    for rec in env.manager.sessions.values():
        if not rec.run or rec.status != "running":
            continue
        try:
            if rec.run.stream.get_chat_id() == env.chat_id:
                has_running_in_chat = True
                break
        except Exception:
            continue

    if not has_running_in_chat:
        # Clear the stored panel id in-memory so PanelUI.ensure_panel will send a new message.
        # If rendering fails, we restore the previous id to avoid "losing" the panel.
        env.manager.panel_by_chat.pop(env.chat_id, None)
    try:
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
    except Exception as e:
        if (
            not has_running_in_chat
            and old_panel_id is not None
            and env.manager.get_panel_message_id(env.chat_id) is None
        ):
            env.manager.panel_by_chat[env.chat_id] = old_panel_id
        _log_error("cmd_start failed.", e)
        return

    if not has_running_in_chat and old_panel_id:
        new_panel_id = env.manager.get_panel_message_id(env.chat_id)
        if new_panel_id and new_panel_id != old_panel_id:
            await env.panel.delete_message_best_effort(chat_id=env.chat_id, message_id=old_panel_id)

    await _delete_user_message_best_effort(update, authorized=True)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    env = await _get_handler_env(update, context, delete_user_message=False)
    if not env:
        return
    _ui_nav_reset(context.chat_data)
    _ui_set(context.chat_data, mode="sessions")
    # Same semantics as /start: treat as a "reset" that should always show a visible panel.
    old_panel_id = env.manager.get_panel_message_id(env.chat_id)
    has_running_in_chat = False
    for rec in env.manager.sessions.values():
        if not rec.run or rec.status != "running":
            continue
        try:
            if rec.run.stream.get_chat_id() == env.chat_id:
                has_running_in_chat = True
                break
        except Exception:
            continue

    if not has_running_in_chat:
        env.manager.panel_by_chat.pop(env.chat_id, None)
    try:
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
    except Exception as e:
        if (
            not has_running_in_chat
            and old_panel_id is not None
            and env.manager.get_panel_message_id(env.chat_id) is None
        ):
            env.manager.panel_by_chat[env.chat_id] = old_panel_id
        _log_error("cmd_menu failed.", e)
        return

    if not has_running_in_chat and old_panel_id:
        new_panel_id = env.manager.get_panel_message_id(env.chat_id)
        if new_panel_id and new_panel_id != old_panel_id:
            await env.panel.delete_message_best_effort(chat_id=env.chat_id, message_id=old_panel_id)

    await _delete_user_message_best_effort(update, authorized=True)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    env = await _get_handler_env(update, context)
    if not env:
        return
    _ui_set(context.chat_data, mode="sessions")
    await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)


async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg_text = update.message.text if update.message else ""
    env = await _get_handler_env(update, context)
    if not env:
        return

    tokens = _parse_tokens(msg_text or "")
    if len(tokens) != 2:
        _ui_set(context.chat_data, mode="sessions", notice="Usage: /use <name>")
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
        return
    name = tokens[1]
    if name not in env.manager.sessions:
        _ui_set(context.chat_data, mode="sessions", notice=f"Unknown session: {name}")
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
        return

    _ui_set(context.chat_data, mode="session", session=name)
    await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg_text = update.message.text if update.message else ""
    env = await _get_handler_env(update, context)
    if not env:
        return

    tokens = _parse_tokens(msg_text or "")
    if len(tokens) >= 3:
        name = tokens[1]
        path = tokens[2]
        engine = tokens[3] if len(tokens) >= 4 else None
        rec, err = await env.manager.create_session(name=name, path=path, engine=engine)
        if err:
            _ui_set(context.chat_data, mode="new_name", notice=err)
            await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
            return
        _ui_set(context.chat_data, mode="session", session=rec.name)
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
        return

    _ui_set(context.chat_data, mode="new_name", new={})
    await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg_text = update.message.text if update.message else ""
    env = await _get_handler_env(update, context)
    if not env:
        return

    tokens = _parse_tokens(msg_text or "")
    ui = _ui_get(context.chat_data)
    fallback = ui.get("session") if isinstance(ui.get("session"), str) else None
    target = tokens[1] if len(tokens) >= 2 else fallback
    if not isinstance(target, str) or not target:
        _ui_set(context.chat_data, mode="sessions", notice="No session selected to stop.")
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
        return

    rec = env.manager.sessions.get(target)
    if not rec:
        _ui_set(context.chat_data, mode="sessions", notice=f"Unknown session: {target}")
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
        return

    if not _is_running(rec):
        _ui_set(context.chat_data, mode="session", session=rec.name, notice="This session is not running.")
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
        return

    await env.manager.stop(rec.name)
    if rec.run and rec.run.paused:
        _ui_set(context.chat_data, mode="session", session=rec.name, notice="Stop requested…")
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg_text = update.message.text if update.message else ""
    env = await _get_handler_env(update, context)
    if not env:
        return

    tokens = _parse_tokens(msg_text or "")
    ui = _ui_get(context.chat_data)
    fallback = ui.get("session") if isinstance(ui.get("session"), str) else None
    target = tokens[1] if len(tokens) >= 2 else fallback
    if not isinstance(target, str) or not target:
        _ui_set(context.chat_data, mode="sessions", notice="No session selected. Use /logs <name>.")
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
        return

    if target not in env.manager.sessions:
        _ui_set(context.chat_data, mode="sessions", notice=f"Unknown session: {target}")
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
        return

    _ui_set(context.chat_data, mode="logs", session=target)
    await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)


def _resolve_session_for_callback_message(
    manager: SessionManager,
    *,
    chat_id: int,
    message_id: Optional[int],
    fallback: Optional[str],
) -> Optional[str]:
    if message_id is None:
        return fallback
    return (
        manager.resolve_attached_running_session_for_message(chat_id=chat_id, message_id=message_id)
        or manager.resolve_session_for_run_message(chat_id=chat_id, message_id=message_id)
        or fallback
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    manager: SessionManager = context.application.bot_data["manager"]
    panel: PanelUI = context.application.bot_data["panel"]

    query = update.callback_query
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not query or chat_id is None:
        return
    data = query.data or ""
    msg_id = query.message.message_id if query.message else None
    _log_line(f"callback chat_id={chat_id} message_id={msg_id} data={data!r}")

    # Best-effort adopt panel message from callback origin (useful after restart).
    # Skip for ephemeral notifications (e.g. "ack"), so we don't overwrite the real panel id.
    cb_action = ""
    if data.startswith(CB_PREFIX + ":"):
        parts_preview = data.split(":")
        cb_action = parts_preview[1] if len(parts_preview) >= 2 else ""
    if query.message and manager.get_panel_message_id(chat_id) is None and cb_action != "ack":
        try:
            await manager.set_panel_message_id(chat_id, query.message.message_id)
        except Exception:
            pass

    try:
        await query.answer()
    except TelegramError:
        pass
    except Exception as e:
        _log_error("Failed to answer callback query.", e)

    if not await _ensure_authorized(update, context):
        return

    if not data.startswith(CB_PREFIX + ":"):
        return

    parts = data.split(":")
    action = parts[1] if len(parts) >= 2 else ""
    arg = parts[2] if len(parts) >= 3 else None

    ui = _ui_get(context.chat_data)
    ui_session = ui.get("session") if isinstance(ui.get("session"), str) else None

    async def _auto_detach_if_running() -> None:
        if not query.message:
            return
        session_name = manager.resolve_attached_running_session_for_message(chat_id=chat_id, message_id=query.message.message_id)
        if not session_name:
            session_name = manager.resolve_session_for_run_message(chat_id=chat_id, message_id=query.message.message_id)
        if not session_name:
            return
        rec_run = manager.sessions.get(session_name)
        if not rec_run or not _is_running(rec_run) or not rec_run.run:
            return
        if rec_run.run.paused:
            return
        rec_run.run.paused = True
        await rec_run.run.stream.pause()

    async def _rerender() -> None:
        await _render_and_sync(manager, panel, context=context, chat_id=chat_id)

    if action not in {"stop", "stop_yes", "stop_no", "interrupt", "detach"}:
        try:
            await _auto_detach_if_running()
        except Exception as e:
            _log_error("auto_detach_if_running failed.", e)

    if action in {"session_back", "new_back", "new_cancel", "paths_back", "await_cancel"}:
        action = "back"

    if action == "ack":
        if query.message:
            await panel.delete_message_best_effort(chat_id=chat_id, message_id=query.message.message_id)
        return

    if action == "home":
        _ui_nav_reset(context.chat_data)
        _ui_set(context.chat_data, mode="sessions")
        await _rerender()
    elif action == "back":
        if not _ui_nav_pop(context.chat_data):
            _ui_set(context.chat_data, mode="sessions")
        _ui_sanitize(manager, context.chat_data)
        await _rerender()
    elif action == "back_sessions":
        session_name_detach = _resolve_session_for_callback_message(
            manager,
            chat_id=chat_id,
            message_id=(query.message.message_id if query.message else None),
            fallback=ui_session,
        )
        rec_detach = manager.sessions.get(session_name_detach) if isinstance(session_name_detach, str) else None
        if rec_detach and _is_running(rec_detach) and rec_detach.run:
            rec_detach.run.paused = True
            await rec_detach.run.stream.pause()
        _ui_nav_reset(context.chat_data)
        _ui_set(context.chat_data, mode="sessions")
        await _rerender()
    elif action == "sessions":
        _ui_nav_to(context.chat_data, mode="sessions")
        await _rerender()
    elif action == "restart":
        running = [
            name
            for name, rec in manager.sessions.items()
            if rec.run and getattr(rec.run.process, "returncode", None) is None
        ]
        if running:
            _ui_set(context.chat_data, notice="Stop all running sessions before restarting the bot.")
            await _rerender()
            return

        restart_event = context.application.bot_data.get("restart_event")
        if not isinstance(restart_event, asyncio.Event):
            _ui_set(context.chat_data, notice="Restart is not available in this environment.")
            await _rerender()
            return

        _ui_set(context.chat_data, mode="sessions", notice="Restarting…")
        await _rerender()

        async def _schedule_restart() -> None:
            await asyncio.sleep(0.25)
            restart_event.set()

        asyncio.create_task(_schedule_restart())
        return
    elif action in {"session", "session_back"}:
        session_name_open = arg if isinstance(arg, str) and arg else ui_session
        if isinstance(session_name_open, str) and session_name_open in manager.sessions:
            _ui_nav_to(context.chat_data, mode="session", session=session_name_open)
        else:
            _ui_nav_to(context.chat_data, mode="sessions", notice="No session selected.")
        await _rerender()
    elif action == "sess":
        try:
            idx = int(arg or "-1")
        except Exception:
            idx = -1
        names = ui.get("sess_list")
        if not isinstance(names, list):
            names = sorted(manager.sessions.keys())
        if idx < 0 or idx >= len(names):
            _ui_set(context.chat_data, mode="sessions", notice="Stale session list. Refreshing…")
            await _rerender()
        else:
            name = str(names[idx])
            if name not in manager.sessions:
                _ui_set(context.chat_data, mode="sessions", notice="Session not found. Refreshing…")
                await _rerender()
            else:
                _ui_nav_to(context.chat_data, mode="session", session=name)
                await _rerender()
    elif action == "new":
        _ui_nav_to(context.chat_data, mode="new_name", new={})
        await _rerender()
    elif action == "new_auto":
        auto_name = ui.get("auto_name") if isinstance(ui.get("auto_name"), str) else manager.next_auto_session_name()
        if auto_name in manager.sessions:
            _ui_set(context.chat_data, mode="new_name", notice="Auto-name is taken. Pick another.")
            await _rerender()
        else:
            _ui_nav_to(context.chat_data, mode="new_engine", new={"name": auto_name})
            await _rerender()
    elif action == "engine":
        engine_val = (arg or "").strip()
        if engine_val not in ENGINE_CHOICES:
            _ui_set(context.chat_data, mode="new_engine", notice="Pick an engine.")
            await _rerender()
        else:
            draft = ui.get("new") if isinstance(ui.get("new"), dict) else {}
            draft["engine"] = engine_val
            _log_line(f"engine_selected engine={engine_val}")
            _ui_nav_to(context.chat_data, mode="new_path", new=draft)
            await _rerender()
    elif action == "path_mode":
        mode_val = (arg or "").strip()
        if mode_val not in {"docs", "full", "reset"}:
            _ui_set(context.chat_data, mode="new_path", notice="Выбери вариант ниже.")
            await _rerender()
        else:
            draft = ui.get("new") if isinstance(ui.get("new"), dict) else {}
            if mode_val == "reset":
                draft.pop("path_mode", None)
            else:
                draft["path_mode"] = mode_val
            _ui_nav_to(context.chat_data, mode="new_path", new=draft)
            await _rerender()
    elif action == "path_pick":
        draft = ui.get("new")
        name = draft.get("name") if isinstance(draft, dict) else None
        engine = draft.get("engine") if isinstance(draft, dict) else None
        if not isinstance(name, str) or not name:
            _ui_set(context.chat_data, mode="new_name", notice="Missing draft name. Start again.")
            await _rerender()
        else:
            try:
                idx = int(arg or "-1")
            except Exception:
                idx = -1
            if idx < 0 or idx >= len(manager.path_presets):
                _ui_set(context.chat_data, mode="new_path", notice="Invalid preset index.")
                await _rerender()
            else:
                preset = manager.path_presets[idx]
                resolved_p, err = _safe_resolve_path(preset)
                if err:
                    _ui_set(context.chat_data, mode="new_path", notice=err, notice_code=preset)
                    await _rerender()
                elif not resolved_p.exists() or not resolved_p.is_dir():
                    _ui_set(context.chat_data, mode="new_path", notice="Папка не найдена.", notice_code=str(resolved_p))
                    await _rerender()
                else:
                    rec, err = await manager.create_session(name=name, path=str(resolved_p), engine=engine)
                    if err:
                        _ui_set(context.chat_data, mode="new_path", notice=err, new={"name": name})
                        await _rerender()
                    else:
                        _ui_nav_reset(context.chat_data, to={"mode": "sessions"})
                        _ui_set(context.chat_data, mode="session", session=rec.name)
                        ui.pop("new", None)
                        await _rerender()
    elif action == "paths":
        _ui_nav_to(context.chat_data, mode="paths")
        await _rerender()
    elif action == "paths_add":
        _ui_nav_to(context.chat_data, mode="paths_add")
        await _rerender()
    elif action == "path_del":
        try:
            idx = int(arg or "-1")
        except Exception:
            idx = -1
        ok = await manager.delete_path_preset(idx)
        _ui_set(context.chat_data, mode="paths", notice="Deleted." if ok else "Invalid preset index.")
        await _rerender()
    elif action == "logs":
        session_name = ui.get("session")
        if not isinstance(session_name, str) or session_name not in manager.sessions:
            _ui_nav_to(context.chat_data, mode="sessions", notice="No session selected.")
        else:
            _ui_nav_to(context.chat_data, mode="logs", session=session_name)
        await _rerender()
    elif action == "info":
        session_name_info = _resolve_session_for_callback_message(
            manager,
            chat_id=chat_id,
            message_id=(query.message.message_id if query.message else None),
            fallback=ui_session,
        )
        if not isinstance(session_name_info, str) or session_name_info not in manager.sessions:
            _ui_nav_to(context.chat_data, mode="sessions", notice="No session selected.")
        else:
            _ui_nav_to(context.chat_data, mode="info", session=session_name_info)
        await _rerender()
    elif action == "log":
        session_name = ui.get("session")
        rec = manager.sessions.get(session_name) if isinstance(session_name, str) else None
        if not rec:
            _ui_nav_to(context.chat_data, mode="sessions", notice="No session selected.")
            await _rerender()
            return

        if _is_running(rec) and rec.run:
            if query.message:
                try:
                    await manager.pause_other_attached_runs(
                        chat_id=chat_id,
                        message_id=query.message.message_id,
                        except_session=rec.name,
                    )
                except Exception as e:
                    _log_error("pause_other_attached_runs failed (log->attach).", e)
                manager.register_run_message(chat_id=chat_id, message_id=query.message.message_id, session_name=rec.name)

            rec.run.paused = False
            def _working_footer_html() -> str:
                elapsed_s = int(time.monotonic() - rec.run.started_mono)
                return f"<code>---- Working {_h(_format_duration(elapsed_s))} ----</code>"

            await rec.run.stream.set_footer(
                footer_provider=_working_footer_html,
                footer_plain_len=len("---- Working 0m 0s ----"),
                wrap_log_in_pre=True,
            )
            await rec.run.stream.set_reply_markup(
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("⬅️", callback_data=_cb("back_sessions")),
                            InlineKeyboardButton("⛔", callback_data=_cb("interrupt")),
                        ]
                    ]
                )
            )
            await rec.run.stream.resume()
            return

        _ui_nav_to(context.chat_data, mode="logs", session=rec.name)
        await _rerender()
    elif action == "disconnect":
        # Legacy: older panels may still have a Disconnect button.
        _ui_nav_reset(context.chat_data)
        _ui_set(context.chat_data, mode="sessions")
        await _rerender()
    elif action in {"start", "run", "continue", "newprompt"}:
        session_name = ui.get("session")
        if not isinstance(session_name, str) or session_name not in manager.sessions:
            _ui_set(context.chat_data, mode="sessions", notice="No session selected.")
            await _rerender()
        else:
            _ui_set(context.chat_data, mode="session", session=session_name)
            await _rerender()
    elif action == "model":
        session_name3 = ui.get("session")
        if not isinstance(session_name3, str) or session_name3 not in manager.sessions:
            _ui_set(context.chat_data, mode="sessions", notice="No session selected.")
        else:
            _ui_nav_to(context.chat_data, mode="model", session=session_name3)
        await _rerender()
    elif action == "model_default":
        _ui_set(context.chat_data, notice="Default model selection is disabled.")
        await _rerender()
    elif action in {"reasoning_default", "verbosity_default"}:
        _ui_set(context.chat_data, notice="Default reasoning option is disabled.")
        await _rerender()
    elif action == "model_pick":
        session_name5 = ui.get("session")
        rec2 = manager.sessions.get(session_name5) if isinstance(session_name5, str) else None
        if not rec2:
            _ui_set(context.chat_data, mode="sessions", notice="No session selected.")
            await _rerender()
        else:
            if rec2.engine == ENGINE_CLAUDE:
                _ui_set(context.chat_data, mode="model", notice="Model presets are not available for Claude.")
                await _rerender()
                return
            try:
                idx = int(arg or "-1")
            except Exception:
                idx = -1
            if idx < 0 or idx >= len(MODEL_PRESETS):
                _ui_set(context.chat_data, mode="model", notice="Invalid model.")
            else:
                rec2.model = MODEL_PRESETS[idx]
                await manager.save_state()
                _ui_set(context.chat_data, mode="model", session=rec2.name, notice=f"Model: {rec2.model}")
            await _rerender()
    elif action in {"reasoning_pick", "verbosity_pick"}:
        session_name5b = ui.get("session")
        rec_v2 = manager.sessions.get(session_name5b) if isinstance(session_name5b, str) else None
        if not rec_v2:
            _ui_set(context.chat_data, mode="sessions", notice="No session selected.")
            await _rerender()
        else:
            if rec_v2.engine == ENGINE_CLAUDE:
                _ui_set(context.chat_data, mode="model", notice="Reasoning effort is not used for Claude.")
                await _rerender()
                return
            level = (arg or "").strip()
            if level not in {"low", "medium", "high", "xhigh"}:
                _ui_set(context.chat_data, mode="model", notice="Invalid reasoning effort.")
            else:
                rec_v2.reasoning_effort = level
                await manager.save_state()
                _ui_set(
                    context.chat_data,
                    mode="model",
                    session=rec_v2.name,
                    notice=f"Reasoning effort: {level}",
                )
            await _rerender()
    elif action == "model_custom":
        _ui_nav_to(context.chat_data, mode="model_custom")
        await _rerender()
    elif action == "delete":
        session_name7 = ui.get("session")
        if not isinstance(session_name7, str) or session_name7 not in manager.sessions:
            _ui_set(context.chat_data, mode="sessions", notice="No session selected.")
        else:
            _ui_set(context.chat_data, mode="confirm_delete", session=session_name7)
        await _rerender()
    elif action == "delete_no":
        session_name8 = ui.get("session")
        if isinstance(session_name8, str) and session_name8 in manager.sessions:
            _ui_set(context.chat_data, mode="session", session=session_name8)
        else:
            _ui_set(context.chat_data, mode="sessions")
        await _rerender()
    elif action == "delete_yes":
        session_name9 = ui.get("session")
        if not isinstance(session_name9, str) or session_name9 not in manager.sessions:
            _ui_set(context.chat_data, mode="sessions", notice="No session selected.")
            await _rerender()
        else:
            ok, msg = await manager.delete_session(session_name9)
            if session_name9 in manager.sessions:
                _ui_set(context.chat_data, mode="session", session=session_name9, notice=msg)
            else:
                _ui_set(context.chat_data, mode="sessions", notice=msg)
            await _rerender()
    elif action == "mkdir_no":
        ui.pop("mkdir", None)
        if not _ui_nav_pop(context.chat_data):
            _ui_set(context.chat_data, mode="sessions")
        await _rerender()
    elif action == "mkdir_yes":
        mkdir = ui.get("mkdir")
        path = mkdir.get("path") if isinstance(mkdir, dict) else None
        flow = mkdir.get("flow") if isinstance(mkdir, dict) else None

        if not isinstance(path, str) or not path:
            _ui_set(context.chat_data, mode="sessions", notice="No pending directory to create.")
            await _rerender()
            return

        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            p = Path(path)
            if not p.exists() or not p.is_dir():
                raise OSError("not a directory after mkdir")
        except Exception as e:
            _ui_set(context.chat_data, mode="confirm_mkdir", notice=f"Failed to create directory: {e}")
            await _rerender()
            return
        _log_line(f"mkdir_created path={path}")

        if flow == "new_path":
            draft = ui.get("new")
            name = draft.get("name") if isinstance(draft, dict) else None
            engine = draft.get("engine") if isinstance(draft, dict) else None
            if not isinstance(name, str) or not name:
                ui.pop("mkdir", None)
                _ui_set(context.chat_data, mode="new_name", notice="Missing draft name. Start again.")
                await _rerender()
                return
            rec, err = await manager.create_session(name=name, path=path, engine=engine)
            if err:
                _ui_set(context.chat_data, mode="new_path", notice=err, new={"name": name})
                ui.pop("mkdir", None)
                await _rerender()
                return
            ui.pop("mkdir", None)
            ui.pop("new", None)
            _ui_nav_reset(context.chat_data, to={"mode": "sessions"})
            _ui_set(context.chat_data, mode="session", session=rec.name)
            await _rerender()
            return

        if flow == "paths_add":
            await manager.upsert_path_preset(path)
            ui.pop("mkdir", None)
            _ui_set(context.chat_data, mode="paths", notice="Added.")
            await _rerender()
            return

        _ui_set(context.chat_data, mode="sessions", notice="Unknown mkdir flow.")
        ui.pop("mkdir", None)
        await _rerender()
    elif action == "clear":
        session_name_clear = ui.get("session")
        if not isinstance(session_name_clear, str) or session_name_clear not in manager.sessions:
            _ui_set(context.chat_data, mode="sessions", notice="No session selected.")
            await _rerender()
        else:
            ok, msg = await manager.clear_session_state(session_name_clear)
            if ok:
                _ui_set(context.chat_data, mode="session", session=session_name_clear, notice=msg)
            else:
                _ui_set(context.chat_data, notice=msg)
            await _rerender()
    elif action in {"stop", "interrupt", "stop_yes"}:
        session_name10 = _resolve_session_for_callback_message(
            manager,
            chat_id=chat_id,
            message_id=(query.message.message_id if query.message else None),
            fallback=ui_session,
        )
        rec3 = manager.sessions.get(session_name10) if isinstance(session_name10, str) else None
        if not rec3 or not _is_running(rec3) or not rec3.run:
            _ui_set(context.chat_data, notice="Not running.")
            await _rerender()
        else:
            await manager.stop(rec3.name)
            if rec3.run.paused:
                _ui_set(context.chat_data, mode="session", session=rec3.name, notice="Stop requested…")
                await _rerender()
            else:
                return
    elif action == "stop_no":
        session_name11 = _resolve_session_for_callback_message(
            manager,
            chat_id=chat_id,
            message_id=(query.message.message_id if query.message else None),
            fallback=ui_session,
        )
        rec4 = manager.sessions.get(session_name11) if isinstance(session_name11, str) else None
        if rec4 and _is_running(rec4) and rec4.run:
            rec4.run.paused = False
            await rec4.run.stream.resume()
            return
        _ui_set(context.chat_data, notice="Not running.")
        await _rerender()
    elif action == "detach":
        session_name13 = _resolve_session_for_callback_message(
            manager,
            chat_id=chat_id,
            message_id=(query.message.message_id if query.message else None),
            fallback=ui_session,
        )
        rec6 = manager.sessions.get(session_name13) if isinstance(session_name13, str) else None
        if rec6 and _is_running(rec6) and rec6.run:
            rec6.run.paused = True
            await rec6.run.stream.pause()
        _ui_nav_reset(context.chat_data)
        _ui_set(context.chat_data, mode="sessions")
        await _rerender()
    elif action == "attach":
        session_name14 = ui.get("session")
        rec7 = manager.sessions.get(session_name14) if isinstance(session_name14, str) else None
        if rec7 and _is_running(rec7) and rec7.run:
            if query.message:
                try:
                    await manager.pause_other_attached_runs(
                        chat_id=chat_id,
                        message_id=query.message.message_id,
                        except_session=rec7.name,
                    )
                except Exception as e:
                    _log_error("pause_other_attached_runs failed (attach).", e)
                manager.register_run_message(chat_id=chat_id, message_id=query.message.message_id, session_name=rec7.name)
            rec7.run.paused = False

            def _working_footer_html() -> str:
                elapsed_s = int(time.monotonic() - rec7.run.started_mono)
                return f"<code>---- Working {_h(_format_duration(elapsed_s))} ----</code>"

            await rec7.run.stream.set_footer(
                footer_provider=_working_footer_html,
                footer_plain_len=len("---- Working 0m 0s ----"),
                wrap_log_in_pre=True,
            )
            await rec7.run.stream.set_reply_markup(
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("⬅️", callback_data=_cb("back_sessions")),
                            InlineKeyboardButton("⛔", callback_data=_cb("interrupt")),
                        ]
                    ]
                )
            )
            await rec7.run.stream.resume()
        else:
            _ui_set(context.chat_data, mode="sessions", notice="Run is not active.")
            await _rerender()
    else:
        _ui_set(context.chat_data, mode="sessions", notice="Unknown action.")
        await _rerender()

    # Best-effort cleanup: if user clicked a stale panel, try deleting it.
    current_panel_id = manager.get_panel_message_id(chat_id)
    if query.message and current_panel_id and query.message.message_id != current_panel_id:
        if manager.resolve_session_for_run_message(chat_id=chat_id, message_id=query.message.message_id):
            return
        try:
            await panel.delete_message_best_effort(chat_id=chat_id, message_id=query.message.message_id)
        except Exception as e:
            _log_error("Failed to delete stale panel message.", e)


async def _schedule_prompt_run(
    *,
    manager: SessionManager,
    panel: PanelUI,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    session_name: str,
    prompt: str,
    ui_mode: str,
    run_mode: str,
) -> None:
    if not isinstance(prompt, str) or not prompt.strip():
        return

    rec = manager.sessions.get(session_name)
    if not rec:
        _ui_set(context.chat_data, mode="sessions", notice="No session selected.")
        await _render_and_sync(manager, panel, context=context, chat_id=chat_id)
        return

    if rec and _is_running(rec):
        return

    if ui_mode == "session":

        async def _run_in_background() -> None:
            try:
                panel_id = await panel.ensure_panel(chat_id)
                await manager.run_prompt(
                    chat_id=chat_id,
                    panel_message_id=panel_id,
                    application=context.application,
                    session_name=session_name,
                    prompt=prompt,
                    run_mode="continue",
                )
            except Exception as e:
                print(f"run_prompt failed: {e}", file=sys.stderr)

        asyncio.create_task(_run_in_background())
        return

    if ui_mode != "await_prompt":
        return

    if run_mode not in {"continue", "new"}:
        run_mode = "new"

    ui = _ui_get(context.chat_data)
    prior_notice = ui.get("notice") if isinstance(ui.get("notice"), str) else ""
    starting_notice = "Starting… (see output message below)"
    if prior_notice and prior_notice.strip() and prior_notice.strip() != starting_notice:
        starting_notice = f"{prior_notice.strip()}\n\n{starting_notice}"

    _ui_set(context.chat_data, mode="session", session=session_name, notice=starting_notice)
    await _render_and_sync(manager, panel, context=context, chat_id=chat_id)

    async def _run_and_refresh() -> None:
        try:
            panel_id = await panel.ensure_panel(chat_id)
            await manager.run_prompt(
                chat_id=chat_id,
                panel_message_id=panel_id,
                application=context.application,
                session_name=session_name,
                prompt=prompt,
                run_mode=run_mode,
            )
        except Exception as e:
            print(f"run_prompt failed: {e}", file=sys.stderr)
        finally:
            ui2 = _ui_get(context.chat_data)
            mode2 = ui2.get("mode") if isinstance(ui2.get("mode"), str) else "sessions"
            session2 = ui2.get("session") if isinstance(ui2.get("session"), str) else None

            if mode2 == "await_prompt":
                if session_name in manager.sessions:
                    _ui_set(context.chat_data, mode="session", session=session_name, notice="Run finished.")
                else:
                    _ui_set(context.chat_data, mode="sessions", notice="Run finished.")
            elif mode2 == "session" and session2 == session_name:
                _ui_set(context.chat_data, notice="Run finished.")
            else:
                _ui_set(context.chat_data, notice=f"Run finished: {session_name}")

    asyncio.create_task(_run_and_refresh())


async def _flush_media_group(
    *,
    manager: SessionManager,
    panel: PanelUI,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    media_group_id: str,
) -> None:
    if not isinstance(media_group_id, str) or not media_group_id:
        return

    while True:
        await asyncio.sleep(MEDIA_GROUP_DEBOUNCE_SECONDS)
        groups = context.chat_data.get("_media_groups")
        if not isinstance(groups, dict):
            return
        group = groups.get(media_group_id)
        if not isinstance(group, dict):
            return
        last = group.get("last_update_mono")
        last_mono = float(last) if isinstance(last, (int, float)) else 0.0
        if (time.monotonic() - last_mono) < MEDIA_GROUP_DEBOUNCE_SECONDS:
            continue

        groups.pop(media_group_id, None)

        session_name = group.get("session_name")
        ui_mode = group.get("ui_mode")
        run_mode = group.get("run_mode")
        user_text = group.get("user_text")
        filenames = group.get("filenames")

        if not isinstance(session_name, str) or not session_name:
            return
        ui_mode2 = ui_mode if isinstance(ui_mode, str) else "session"
        run_mode2 = run_mode if isinstance(run_mode, str) else "continue"
        prompt = _build_prompt_with_downloaded_files(
            user_text=(user_text if isinstance(user_text, str) else ""),
            filenames=(filenames if isinstance(filenames, list) else []),
        )
        await _schedule_prompt_run(
            manager=manager,
            panel=panel,
            context=context,
            chat_id=chat_id,
            session_name=session_name,
            prompt=prompt,
            ui_mode=ui_mode2,
            run_mode=run_mode2,
        )
        return


async def on_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    env = await _get_handler_env(update, context)
    if not env or not update.message:
        return

    ui = _ui_get(context.chat_data)
    mode = ui.get("mode") if isinstance(ui.get("mode"), str) else "sessions"

    ui_mode = mode
    session_name: Optional[str] = None
    run_mode = "continue"

    if mode == "session":
        session_name = ui.get("session") if isinstance(ui.get("session"), str) else None
        run_mode = "continue"
    elif mode == "await_prompt":
        session_name = ui.get("session") if isinstance(ui.get("session"), str) else None
        await_prompt = ui.get("await_prompt")
        run_mode = await_prompt.get("run_mode") if isinstance(await_prompt, dict) else "new"
        if run_mode not in {"continue", "new"}:
            run_mode = "new"
    else:
        _ui_set(context.chat_data, notice="Select a session first.")
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
        return

    if not session_name or session_name not in env.manager.sessions:
        _ui_set(context.chat_data, mode="sessions", notice="No session selected.")
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
        return

    rec = env.manager.sessions.get(session_name)
    if not rec:
        return

    caption = (getattr(update.message, "caption", None) or "").strip()

    try:
        filenames, notice = await _download_attachments_to_session_root(
            message=update.message,
            bot=context.application.bot,
            session_root=Path(rec.path),
        )
    except Exception as e:
        _ui_set(context.chat_data, notice=f"Failed to download attachment: {e}")
        await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
        return

    if notice:
        _ui_set(context.chat_data, notice=notice)
        # For session mode, `_schedule_prompt_run` won't render anything before starting the run,
        # so we render the notice explicitly.
        if ui_mode == "session":
            await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)
        if not filenames:
            return

    if not filenames:
        return

    media_group_id = getattr(update.message, "media_group_id", None)
    if isinstance(media_group_id, str) and media_group_id:
        groups = context.chat_data.get("_media_groups")
        if not isinstance(groups, dict):
            groups = {}
            context.chat_data["_media_groups"] = groups

        group = groups.get(media_group_id)
        if not isinstance(group, dict):
            group = {
                "session_name": session_name,
                "ui_mode": ui_mode,
                "run_mode": run_mode,
                "user_text": "",
                "filenames": [],
                "last_update_mono": time.monotonic(),
            }
            groups[media_group_id] = group
            group["task"] = asyncio.create_task(
                _flush_media_group(
                    manager=env.manager,
                    panel=env.panel,
                    context=context,
                    chat_id=env.chat_id,
                    media_group_id=media_group_id,
                )
            )

        files_list = group.get("filenames")
        if not isinstance(files_list, list):
            files_list = []
            group["filenames"] = files_list
        files_list.extend(filenames)

        if caption:
            current_text = group.get("user_text")
            if not isinstance(current_text, str) or not current_text.strip():
                group["user_text"] = caption

        group["last_update_mono"] = time.monotonic()
        return

    prompt = _build_prompt_with_downloaded_files(user_text=caption, filenames=filenames)
    await _schedule_prompt_run(
        manager=env.manager,
        panel=env.panel,
        context=context,
        chat_id=env.chat_id,
        session_name=session_name,
        prompt=prompt,
        ui_mode=ui_mode,
        run_mode=run_mode,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    manager: SessionManager = context.application.bot_data["manager"]
    panel: PanelUI = context.application.bot_data["panel"]

    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id

    text = (update.message.text or "").strip()

    if not await _ensure_authorized(update, context):
        return
    await _delete_user_message_best_effort(update, authorized=True)
    if not text:
        return

    ui = _ui_get(context.chat_data)
    mode = ui.get("mode") if isinstance(ui.get("mode"), str) else "sessions"

    async def _rerender() -> None:
        await _render_and_sync(manager, panel, context=context, chat_id=chat_id)

    if mode == "new_name":
        safe = _safe_session_name(text)
        if not safe:
            _ui_set(context.chat_data, notice="Invalid name. Allowed: a-zA-Z0-9._- (<=64).")
            await _rerender()
            return
        if safe in manager.sessions:
            _ui_set(context.chat_data, notice="A session with this name already exists.")
            await _rerender()
            return
        _ui_nav_to(context.chat_data, mode="new_engine", new={"name": safe})
        await _rerender()
        return

    if mode == "new_engine":
        engine_text = text.strip().lower()
        engine_val = ""
        if engine_text in {ENGINE_CODEX, "codex-cli", "codex cli"}:
            engine_val = ENGINE_CODEX
        elif engine_text in {ENGINE_CLAUDE, "claude code", "claude-code", "claude"}:
            engine_val = ENGINE_CLAUDE
        if not engine_val:
            _ui_set(context.chat_data, notice="Pick engine: codex or claude.")
            await _rerender()
            return
        draft = ui.get("new") if isinstance(ui.get("new"), dict) else {}
        draft["engine"] = engine_val
        _log_line(f"engine_selected engine={engine_val}")
        _ui_nav_to(context.chat_data, mode="new_path", new=draft)
        await _rerender()
        return

    if mode == "new_path":
        draft = ui.get("new")
        name = draft.get("name") if isinstance(draft, dict) else None
        engine = draft.get("engine") if isinstance(draft, dict) else None
        path_mode = draft.get("path_mode") if isinstance(draft, dict) else None
        if not isinstance(name, str) or not name:
            _ui_set(context.chat_data, mode="new_name", notice="Missing draft name. Start again.")
            await _rerender()
            return
        if path_mode not in {"docs", "full"}:
            _ui_set(context.chat_data, notice="Сначала выбери где работать.")
            await _rerender()
            return
        if path_mode == "docs":
            if not _is_simple_folder_name(text):
                _ui_set(context.chat_data, notice="Нужно только имя папки (без слэшей).")
                await _rerender()
                return
            path_text = str(_default_projects_root() / text.strip())
        else:
            path_text = text
        resolved, err = _safe_resolve_path(path_text)
        if err:
            _ui_set(context.chat_data, notice=err, notice_code=text)
            await _rerender()
            return
        abs_path = str(resolved)
        ui.pop("mkdir", None)
        p = Path(abs_path)
        if p.exists() and not p.is_dir():
            _ui_set(context.chat_data, notice="Это не папка.", notice_code=abs_path)
            await _rerender()
            return
        if not p.exists():
            if _can_create_directory(p):
                _ui_nav_to(context.chat_data, mode="confirm_mkdir", mkdir={"path": abs_path, "flow": "new_path"})
                await _rerender()
                return
            _ui_set(context.chat_data, notice="Папка не найдена.", notice_code=abs_path)
            await _rerender()
            return
        rec, err = await manager.create_session(name=name, path=abs_path, engine=engine)
        if err:
            _ui_set(context.chat_data, notice=err, new={"name": name})
            await _rerender()
            return
        ui.pop("new", None)
        _ui_nav_reset(context.chat_data, to={"mode": "sessions"})
        _ui_set(context.chat_data, mode="session", session=rec.name)
        await _rerender()
        return

    if mode == "paths_add":
        resolved, err = _safe_resolve_path(text)
        if err:
            _ui_set(context.chat_data, notice=err, notice_code=text)
            await _rerender()
            return
        abs_path = str(resolved)
        ui.pop("mkdir", None)
        p = Path(abs_path)
        if p.exists() and not p.is_dir():
            _ui_set(context.chat_data, notice="Это не папка.", notice_code=abs_path)
            await _rerender()
            return
        if not p.exists():
            if _can_create_directory(p):
                _ui_nav_to(context.chat_data, mode="confirm_mkdir", mkdir={"path": abs_path, "flow": "paths_add"})
                await _rerender()
                return
            _ui_set(context.chat_data, notice="Папка не найдена.", notice_code=abs_path)
            await _rerender()
            return
        await manager.upsert_path_preset(abs_path)
        _ui_set(context.chat_data, mode="paths", notice="Added.")
        await _rerender()
        return

    if mode == "model_custom":
        session_name = ui.get("session")
        rec = manager.sessions.get(session_name) if isinstance(session_name, str) else None
        model = text.strip()
        if not rec:
            _ui_set(context.chat_data, mode="sessions", notice="No session selected.")
            await _rerender()
            return
        if not model:
            _ui_set(context.chat_data, notice="Model id can’t be empty.")
            await _rerender()
            return
        rec.model = model
        await manager.save_state()
        _ui_set(context.chat_data, notice=f"Model: {model}")
        if not _ui_nav_pop(context.chat_data):
            _ui_set(context.chat_data, mode="session", session=rec.name)
        await _rerender()
        return

    if mode == "session":
        session_name = ui.get("session")
        if not isinstance(session_name, str) or session_name not in manager.sessions:
            _ui_set(context.chat_data, mode="sessions", notice="No session selected.")
            await _rerender()
            return

        rec = manager.sessions.get(session_name)
        if rec and _is_running(rec):
            # Keep the running panel attached; best-effort ignore extra prompts while running.
            return

        async def _run_in_background() -> None:
            try:
                panel_id = await panel.ensure_panel(chat_id)
                await manager.run_prompt(
                    chat_id=chat_id,
                    panel_message_id=panel_id,
                    application=context.application,
                    session_name=session_name,
                    prompt=text,
                    run_mode="continue",
                )
            except Exception as e:
                print(f"run_prompt failed: {e}", file=sys.stderr)

        asyncio.create_task(_run_in_background())
        return

    if mode == "await_prompt":
        session_name = ui.get("session")
        if not isinstance(session_name, str) or session_name not in manager.sessions:
            _ui_set(context.chat_data, mode="sessions", notice="No session selected.")
            await _rerender()
            return
        rec = manager.sessions.get(session_name)
        if rec and _is_running(rec):
            _ui_set(context.chat_data, mode="session", session=rec.name, notice="This session is already running.")
            await _rerender()
            return

        await_prompt = ui.get("await_prompt")
        run_mode = await_prompt.get("run_mode") if isinstance(await_prompt, dict) else "new"
        if run_mode not in {"continue", "new"}:
            run_mode = "new"

        _ui_set(context.chat_data, mode="session", session=session_name, notice="Starting… (see output message below)")
        await _rerender()

        async def _run_and_refresh() -> None:
            try:
                panel_id = await panel.ensure_panel(chat_id)
                await manager.run_prompt(
                    chat_id=chat_id,
                    panel_message_id=panel_id,
                    application=context.application,
                    session_name=session_name,
                    prompt=text,
                    run_mode=run_mode,
                )
            except Exception as e:
                print(f"run_prompt failed: {e}", file=sys.stderr)
            finally:
                ui2 = _ui_get(context.chat_data)
                mode2 = ui2.get("mode") if isinstance(ui2.get("mode"), str) else "sessions"
                session2 = ui2.get("session") if isinstance(ui2.get("session"), str) else None

                if mode2 == "await_prompt":
                    if session_name in manager.sessions:
                        _ui_set(context.chat_data, mode="session", session=session_name, notice="Run finished.")
                    else:
                        _ui_set(context.chat_data, mode="sessions", notice="Run finished.")
                elif mode2 == "session" and session2 == session_name:
                    _ui_set(context.chat_data, notice="Run finished.")
                else:
                    _ui_set(context.chat_data, notice=f"Run finished: {session_name}")

        asyncio.create_task(_run_and_refresh())
        return

    # _ui_set(context.chat_data, notice="Use the buttons in the panel.")
    await _rerender()


async def on_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    env = await _get_handler_env(update, context)
    if not env:
        return
    # _ui_set(context.chat_data, mode="sessions", notice="Unknown command. Use /start.")
    await _render_and_sync(env.manager, env.panel, context=context, chat_id=env.chat_id)


async def run_bot(*, token: str, admin_id: Optional[int]) -> None:
    _maybe_migrate_runtime_files()
    manager = SessionManager(admin_id=admin_id)

    app = ApplicationBuilder().token(token).build()
    app.bot_data["manager"] = manager
    app.bot_data["panel"] = PanelUI(app, manager)
    app.bot_data["restart_event"] = asyncio.Event()

    async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = getattr(context, "error", None)
        if err:
            _log_error("Unhandled exception in handler.", err)
        else:
            _log_error("Unhandled exception in handler (no error object).")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("stop", cmd_stop))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ATTACHMENT & ~filters.COMMAND, on_attachment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.COMMAND, on_unknown_command))
    app.add_error_handler(_error_handler)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await app.initialize()
    await app.start()
    # Explicitly request all update types so callback buttons keep working even
    # if a previous webhook/polling run restricted allowed_updates.
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    restart_requested = False
    restart_event: asyncio.Event = app.bot_data["restart_event"]
    stop_task = asyncio.create_task(stop_event.wait())
    restart_task = asyncio.create_task(restart_event.wait())
    pending: set[asyncio.Task[Any]] = set()

    try:
        done, pending = await asyncio.wait({stop_task, restart_task}, return_when=asyncio.FIRST_COMPLETED)
        restart_requested = (restart_task in done) and restart_event.is_set() and not stop_event.is_set()
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await manager.shutdown()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

    if restart_requested:
        _log_line("Restart requested; restarting process (execv).")
        os.execv(sys.executable, [sys.executable] + sys.argv)


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Telegram bot: local session manager for Codex CLI")
    p.add_argument("--token", default=None, help="Telegram bot token (or env VIBES_TOKEN/TELEGRAM_BOT_TOKEN)")
    p.add_argument("--admin", type=int, default=None, help="Allowed Telegram user_id (or env VIBES_ADMIN_ID)")
    return p.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    token = args.token or os.environ.get("VIBES_TOKEN") or os.environ.get("VIBES_TELEGRAM_TOKEN") or os.environ.get(
        "TELEGRAM_BOT_TOKEN"
    )
    if not token:
        print(
            "Не задан токен Telegram-бота.\n"
            "Передай `--token ...` или задай env `VIBES_TOKEN=...`.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    admin_id = args.admin
    if admin_id is None:
        raw = os.environ.get("VIBES_ADMIN_ID") or os.environ.get("VIBES_TELEGRAM_ADMIN_ID") or os.environ.get(
            "TELEGRAM_ADMIN_ID"
        )
        if raw:
            try:
                admin_id = int(raw)
            except ValueError:
                admin_id = None
    try:
        asyncio.run(run_bot(token=token, admin_id=admin_id))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
