#!/usr/bin/env bash
set -euo pipefail

_info() { printf '%s\n' "$*"; }
_warn() { printf 'WARN: %s\n' "$*" >&2; }
_die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

_step_total=6
_step_i=0
_step() {
  _step_i=$((_step_i + 1))
  _info ""
  _info "[$_step_i/$_step_total] $1"
}

_tty="/dev/tty"
_read_tty() {
  [[ -r "$_tty" ]] || return 1
  # shellcheck disable=SC2162
  IFS= read "$@" <"$_tty"
}

_read_secret_tty() {
  # Usage: _read_secret_tty VAR PROMPT
  local __var="$1"
  local __prompt="$2"
  local __val=""
  if [[ -r "$_tty" ]]; then
    # shellcheck disable=SC2162
    IFS= read -r -s -p "$__prompt" __val <"$_tty" || return 1
    printf '\n' >"$_tty" || true
    printf -v "$__var" '%s' "$__val"
    return 0
  fi
  return 1
}

_check_cmd() {
  command -v "$1" >/dev/null 2>&1 || _die "Не найдено: $1. Установи и повтори попытку."
}

_check_python() {
  _check_cmd python3
  python3 - <<'PY' >/dev/null 2>&1 || {
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
    _die $'Нужен python3 версии 3.10+.\n\nmacOS (brew):\n  brew install python@3.11\n\nUbuntu/Debian:\n  sudo apt-get update && sudo apt-get install -y python3 python3-venv\n'
}

  python3 - <<'PY' >/dev/null 2>&1 || {
import venv  # noqa: F401
PY
    _die $'В твоём python3 нет модуля venv.\n\nUbuntu/Debian:\n  sudo apt-get update && sudo apt-get install -y python3-venv\n'
}
}

_env_get() {
  # Usage: _env_get KEY FILE
  local key="$1"
  local file="$2"
  [[ -f "$file" ]] || return 1
  local line
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" 2>/dev/null | head -n 1 || true)"
  [[ -n "$line" ]] || return 1
  line="${line#export }"
  line="${line#${key}=}"
  line="${line%$'\r'}"
  if [[ "${line:0:1}" == "'" && "${line: -1}" == "'" ]]; then
    line="${line:1:${#line}-2}"
  elif [[ "${line:0:1}" == '"' && "${line: -1}" == '"' ]]; then
    line="${line:1:${#line}-2}"
  fi
  printf '%s' "$line"
}

_env_set() {
  # Usage: _env_set FILE KEY VALUE
  local file="$1"
  local key="$2"
  local value="$3"

  local tmp
  tmp="$(mktemp "${file}.tmp.XXXXXXXX")"
  if [[ -f "$file" ]]; then
    grep -v -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" >"$tmp" || true
  fi
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  mv "$tmp" "$file"
  chmod 600 "$file" 2>/dev/null || true
}

_pick_rc_file() {
  local shell_name
  shell_name="$(basename "${SHELL:-}")"
  case "$shell_name" in
    zsh) printf '%s' "$HOME/.zshrc" ;;
    bash)
      if [[ -f "$HOME/.bashrc" ]]; then
        printf '%s' "$HOME/.bashrc"
      else
        printf '%s' "$HOME/.bash_profile"
      fi
      ;;
    *) printf '%s' "$HOME/.profile" ;;
  esac
}

_ensure_path_block() {
  local rc_file="$1"
  [[ -n "$rc_file" ]] || return 0
  touch "$rc_file" 2>/dev/null || {
    _warn "Не могу создать/обновить файл: $rc_file"
    return 0
  }
  if grep -q '^# >>> vibes >>>$' "$rc_file" 2>/dev/null; then
    return 0
  fi
  {
    printf '\n# >>> vibes >>>\n'
    printf 'export PATH="$HOME/.local/bin:$PATH"\n'
    printf '# <<< vibes <<<\n'
  } >>"$rc_file" 2>/dev/null || {
    _warn "Не могу записать в $rc_file. Добавь в PATH вручную: export PATH=\"$HOME/.local/bin:\$PATH\""
    return 0
  }
}

_main() {
  local repo_root
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "$repo_root"

  local codex_missing=0

  _step "Проверяю зависимости…"
  _check_cmd git
  _check_python
  if ! command -v codex >/dev/null 2>&1; then
    codex_missing=1
    _warn "Codex CLI (`codex`) не найден в PATH. Это не фатально, но без него бот не сможет запускать сессии."
  fi

  _step "Создаю/обновляю .venv…"
  if [[ -d ".venv" ]]; then
    if ! python3 -m venv --upgrade ".venv" >/dev/null 2>&1; then
      _warn "Не удалось обновить существующую .venv — пробую использовать как есть."
    fi
  else
    python3 -m venv ".venv"
  fi
  local venv_py="$repo_root/.venv/bin/python"
  [[ -x "$venv_py" ]] || _die "Не найден python в venv: $venv_py"

  _step "Ставлю зависимости…"
  "$venv_py" -m pip --version >/dev/null 2>&1 || "$venv_py" -m ensurepip --upgrade >/dev/null 2>&1 || true
  "$venv_py" -m pip install -U pip >/dev/null 2>&1 || true
  "$venv_py" -m pip install -r "$repo_root/requirements.txt"

  _step "Настраиваю .env…"
  local env_path="$repo_root/.env"

  local token="${VIBES_TOKEN:-}"
  if [[ -z "${token}" ]]; then
    token="$(_env_get VIBES_TOKEN "$env_path" || true)"
  fi
  if [[ -z "${token}" ]]; then
    _info "Telegram bot token можно получить в @BotFather."
    if ! _read_secret_tty token "Введи Telegram bot token (VIBES_TOKEN): "; then
      _die "Нет интерактивного TTY. Установи переменную окружения VIBES_TOKEN и запусти снова."
    fi
  fi
  [[ -n "${token}" ]] || _die "Пустой токен. Отмена."
  case "${token}" in *$'\n'*|*$'\r'*) _die "Токен содержит перевод строки. Проверь ввод." ;; esac

  local admin_id="${VIBES_ADMIN_ID:-}"
  if [[ -z "${admin_id}" ]]; then
    admin_id="$(_env_get VIBES_ADMIN_ID "$env_path" || true)"
  fi
  if [[ -z "${admin_id}" ]]; then
    if [[ -r "$_tty" ]]; then
      if _read_tty -r -p "Admin user_id (опционально, Enter чтобы пропустить): " admin_id; then
        :
      else
        admin_id=""
      fi
    fi
  fi
  if [[ -n "${admin_id}" && ! "${admin_id}" =~ ^[0-9]+$ ]]; then
    _die "Admin user_id должен быть числом (или пусто)."
  fi

  _env_set "$env_path" "VIBES_TOKEN" "$token"
  if [[ -n "${admin_id}" ]]; then
    _env_set "$env_path" "VIBES_ADMIN_ID" "$admin_id"
  fi

  _step "Добавляю vibes в PATH…"
  local local_bin="$HOME/.local/bin"
  mkdir -p "$local_bin"
  local link_path="$local_bin/vibes"
  local target_path="$repo_root/vibes"
  if [[ -e "$link_path" && ! -L "$link_path" ]]; then
    _warn "$link_path уже существует и это не symlink — не трогаю. Удали/переименуй файл и запусти setup.sh снова."
  else
    ln -sf "$target_path" "$link_path" 2>/dev/null || {
      _warn "Не удалось создать symlink: $link_path"
      _warn "Можно запускать из репозитория: $target_path (или ./vibes)"
    }
  fi

  local need_rc=0
  case ":${PATH:-}:" in
    *":$HOME/.local/bin:"*) ;;
    *) need_rc=1 ;;
  esac
  local rc_file=""
  if [[ "$need_rc" -eq 1 ]]; then
    rc_file="$(_pick_rc_file)"
    _ensure_path_block "$rc_file"
  fi

  _step "Запускаю бота…"
  ./vibes start --restart

  _info ""
  ./vibes status || true
  _info "Логи: $(./vibes logs 2>/dev/null || printf '%s' "$repo_root/.vibes/daemon.log")"

  _info ""
  _info "Готово: открой Telegram и напиши боту /start"
  _info "Проверить: vibes status"
  _info ""
  _info "Управление:"
  _info "  vibes status"
  _info "  vibes logs -f"
  _info "  vibes stop"
  _info "  vibes start --restart"
  _info "  vibes help"

  if [[ -n "$rc_file" ]]; then
    _info ""
    _info "PATH обновлён в: $rc_file"
    _info "Перезапусти терминал или выполни:"
    _info "  source \"$rc_file\""
  fi

  if [[ "$codex_missing" -eq 1 ]]; then
    _info ""
    _info "ВАЖНО: без Codex CLI (`codex`) бот запустится, но не сможет запускать сессии."
    _info "Установи `codex` и настрой API key, затем перезапусти бота: vibes start --restart"
  fi
}

_main "$@"
