#!/usr/bin/env bash
set -euo pipefail

_info() { printf '%s\n' "$*"; }
_warn() { printf 'WARN: %s\n' "$*" >&2; }
_die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_repo_root="$_script_dir"

_yes=0
case "${1:-}" in
  --yes|-y) _yes=1 ;;
  "" ) ;;
  * ) _die "Usage: $0 [--yes]" ;;
esac

_remove_symlink() {
  local link_path="$HOME/.local/bin/vibes"
  [[ -L "$link_path" ]] || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  local link_real repo_real
  link_real="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$link_path" 2>/dev/null || true)"
  repo_real="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${_repo_root}/vibes" 2>/dev/null || true)"
  [[ -n "$link_real" && -n "$repo_real" ]] || return 0

  if [[ "$link_real" == "$repo_real" ]]; then
    rm -f "$link_path" || true
    _info "Удалён symlink: $link_path"
  fi
}

_remove_path_block() {
  command -v python3 >/dev/null 2>&1 || return 0
  local rc
  for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    [[ -f "$rc" ]] || continue
    local removed=""
    removed="$(python3 - "$rc" <<'PY' 2>/dev/null || true
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
try:
    lines = path.read_text(encoding="utf-8").splitlines(True)
except FileNotFoundError:
    sys.exit(0)

start = end = None
for i, line in enumerate(lines):
    if line.strip("\r\n") == "# >>> vibes >>>":
        start = i
        continue
    if start is not None and line.strip("\r\n") == "# <<< vibes <<<":
        end = i
        break

if start is None or end is None:
    sys.exit(0)

del lines[start : end + 1]
path.write_text("".join(lines), encoding="utf-8")
print(str(path))
PY
)"
    if [[ -n "$removed" ]]; then
      _info "Удалён PATH-блок из: $removed"
    fi
  done
}

_confirm() {
  [[ "$_yes" -eq 1 ]] && return 0
  _info "Это удалит из репозитория: .venv, .vibes, .env (и уберёт команду vibes из PATH)."
  printf "Продолжить? [y/N] "
  local ans=""
  if [[ -r /dev/tty ]]; then
    # shellcheck disable=SC2162
    IFS= read ans </dev/tty || ans=""
  fi
  case "$ans" in
    y|Y|yes|YES) return 0 ;;
    *) _info "Отмена."; return 1 ;;
  esac
}

_main() {
  cd "$_repo_root"
  [[ -f "$_repo_root/vibes" && -f "$_repo_root/vibes.py" ]] || _die "Не похоже на корень репозитория vibes: $_repo_root"

  _confirm || exit 1

  _info "Останавливаю бота…"
  ./vibes stop >/dev/null 2>&1 || true
  if ./vibes status >/dev/null 2>&1; then
    _die "Бот всё ещё запущен. Останови вручную: ./vibes stop --force (если уверен), затем повтори uninstall."
  fi

  _info "Убираю команду vibes из PATH…"
  _remove_symlink
  _remove_path_block

  _info "Удаляю локальные файлы/папки…"
  rm -rf "$_repo_root/.venv" "$_repo_root/.vibes" 2>/dev/null || true
  rm -f "$_repo_root/.env" 2>/dev/null || true

  _info "Готово. Если хочешь удалить всё полностью — просто удали папку репозитория."
}

_main "$@"
