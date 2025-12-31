# vibes

`vibes.py` — Telegram-бот “session manager” для Codex CLI.  
`vibes` — CLI-скрипт для запуска/статуса/остановки бота в фоне.

## Самый простой старт (3 шага)

1) Клонируй репозиторий и зайди в папку:

```bash
git clone https://github.com/yontare/vibes.git
cd vibes
```

2) Запусти установку (одна команда):

```bash
./setup.sh
```

Если видишь `Permission denied`, сделай:

```bash
chmod +x setup.sh
```

3) Открой Telegram → напиши боту `/start`

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

## Подробности

### Требования

- `python3` версии **3.10+**
- `git`
- `codex` в `PATH` + настроенный API key (без этого бот запустится, но не сможет запускать сессии)

### Как получить Telegram bot token

1) Открой Telegram: `@BotFather`
2) Команда: `/newbot`
3) Скопируй выданный token (это `VIBES_TOKEN`)

### Где лежат конфиг/логи/состояние

- Конфиг: `<repo>/.env`
- Рантайм/стейт: `<repo>/.vibes/daemon.json`
- Логи демона: `<repo>/.vibes/daemon.log` (удобно: `vibes logs -f`)

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
