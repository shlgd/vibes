# vibes

`vibes.py` — Telegram-бот “session manager” для Codex CLI и Claude Code.  
`vibes` — CLI-скрипт для запуска/статуса/остановки бота в фоне.

Это форк с beta-улучшениями UX и поддержкой Claude Code.  
Оригинальный репозиторий и автор — большие молодцы; мы стараемся бережно расширять функционал.

## Самый простой старт (4 шага)

1) Клонируй репозиторий и зайди в папку:

```bash
git clone https://github.com/shlgd/vibes.git
cd vibes
```

2) Скопируй шаблон окружения и заполни токен:

```bash
cp .env.example .env
```

3) Запусти установку (одна команда):

```bash
./setup.sh
```

Если видишь `Permission denied`, сделай:

```bash
chmod +x setup.sh
```

4) Открой Telegram → напиши боту `/start`

Проверить что работает:

```bash
vibes status
```

## Команды (самое нужное)

```bash
vibes status
vibes logs -f
vibes stop
vibes start --restart
vibes help
```

## Что нового в этой beta-ветке

- Выбор движка в мастере создания сессии: **Codex CLI** или **Claude Code**
- Явный выбор пути: **создать в Documents** или **указать полный путь**
- Ответы CLI приходят отдельными сообщениями → история диалога сохраняется
- “Инфо” по сессии теперь в меню (кнопка `ℹ️`), без навязчивых уведомлений

## Подробности

### Требования

- `python3` версии **3.10+**
- `git`
- `codex` в `PATH` + настроенный API key (для Codex-сессий)
- `claude` в `PATH` (для Claude Code-сессий)

### Engines: Codex vs Claude

В мастере создания сессии выбираешь движок:
- **Codex CLI** (по умолчанию)
- **Claude Code**

Для Claude можно задать:
- `VIBES_CLAUDE_MODEL=sonnet`
- `VIBES_CLAUDE_PERMISSION_MODE=bypassPermissions`

### Как получить Telegram bot token

1) Открой Telegram: `@BotFather`
2) Команда: `/newbot`
3) Скопируй выданный token (это `VIBES_TOKEN`)

### Где лежат конфиг/логи/состояние

- Конфиг: `<repo>/.env`
- Рантайм/стейт: `<repo>/.vibes/daemon.json`
- Логи демона: `<repo>/.vibes/daemon.log` (удобно: `vibes logs -f`)

### Выбор пути для сессии

- В шаге “Где работать?” есть два варианта:
  - **Создать в Documents** — ты вводишь только имя папки, она создаётся в `~/Documents`
  - **Указать полный путь** — ты вводишь директорию целиком
- Базовую папку можно переопределить: `VIBES_DEFAULT_PROJECTS_DIR=/path/to/projects`

### Пример .env (шаблон)

Смотри файл `.env.example`. Минимально нужно задать `VIBES_TOKEN`.

```bash
# Telegram bot token (required)
VIBES_TOKEN=

# Your Telegram numeric user_id (optional)
# VIBES_ADMIN_ID=

# Codex CLI settings
VIBES_CODEX_SANDBOX=danger-full-access
VIBES_CODEX_APPROVAL_POLICY=never

# Claude Code settings
VIBES_CLAUDE_MODEL=sonnet
VIBES_CLAUDE_PERMISSION_MODE=bypassPermissions

# Optional: default projects root (overrides ~/Documents)
# VIBES_DEFAULT_PROJECTS_DIR=~/Documents
```

### Uninstall

```bash
./uninstall.sh
```

Без подтверждения:

```bash
./uninstall.sh --yes
```

## Troubleshooting

- `vibes: command not found`: перезапусти терминал или выполни `source ~/.zshrc` / `source ~/.bashrc` / `source ~/.profile`
- Нет `codex` в `PATH`: установи Codex CLI, настрой API key, затем `vibes start --restart`
- Где смотреть логи: `vibes logs -f` (путь также печатает `vibes logs`)
