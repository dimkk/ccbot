# Local Test Guide

## 1. Требования

- Python `3.12+`
- `tmux`
- `uv`
- Telegram bot token и ваш Telegram user id
- CLI провайдер(ы):
  - `claude` (для `CCBOT_PROVIDER=claude`)
  - `codex` (для `CCBOT_PROVIDER=codex`)

## 2. Установка зависимостей

```bash
uv sync --extra dev
```

## 3. Базовая конфигурация

Создайте файл `~/.ccbot/.env`:

```ini
TELEGRAM_BOT_TOKEN=your_bot_token
ALLOWED_USERS=your_telegram_user_id
TMUX_SESSION_NAME=ccbot
```

## 4. Проверка тестов

```bash
uv run pytest -q
uv run ruff check src tests
```

## 5. Локальный запуск (Claude)

```bash
export CCBOT_PROVIDER=claude
export CCBOT_AGENT_COMMAND=claude
# optional backward-compatible alias:
# export CLAUDE_COMMAND=claude

# один раз установить hook
uv run ccbot hook --install

# запуск бота
uv run ccbot
```

Проверить в Telegram:

1. Создать topic
2. Отправить сообщение
3. Выбрать директорию
4. Убедиться, что создаётся tmux window и идут ответы

## 6. Локальный запуск (Codex)

```bash
export CCBOT_PROVIDER=codex
export CCBOT_AGENT_COMMAND=codex
# optional override:
# export CCBOT_CODEX_SESSIONS_PATH=~/.codex/sessions

# запуск бота
uv run ccbot
```

Для ручной синхронизации window->session map:

```bash
uv run ccbot codex-map
```

Проверить в Telegram:

1. Создать topic
2. Отправить сообщение
3. Выбрать директорию
4. Убедиться, что создаётся tmux window с `codex`
5. Убедиться, что новые сообщения приходят из rollout-файлов (`~/.codex/sessions/.../rollout-*.jsonl`)

## 7. Отладка

Проверить локальные state-файлы:

```bash
ls -la ~/.ccbot
cat ~/.ccbot/session_map.json
cat ~/.ccbot/state.json
cat ~/.ccbot/monitor_state.json
```

Проверить tmux:

```bash
tmux ls
tmux attach -t ccbot
```

## 8. Быстрый smoke-check сценарий

1. `uv run pytest -q`
2. `export CCBOT_PROVIDER=codex`
3. `uv run ccbot`
4. В Telegram создать topic и отправить короткий prompt
5. Проверить, что ответ пришёл и `session_map.json` содержит запись с `"provider": "codex"`
6. Повторить для `CCBOT_PROVIDER=claude`
