# Deploy: child_bot (Дневник сна ребенка)

Бот развернут на сервере и работает. Этот документ — как перезапускать/переезжать.

## Текущее состояние (2026-10-28)

- Процесс: `python -m src.main` (PID смотрится `pgrep -f src.main`), лог `/home/openclaw/child_bot-data/bot.log`
- venv: `/home/openclaw/venvs/sleepbot`
- Конфиг: `/home/openclaw/child_bot/.env` (chmod 600; BOT_TOKEN, BOT_DB_PATH, TZ) — токен вне git
- БД: `/home/openclaw/child_bot-data/sleep-diary.db`
- Автозапуск: user-cron `@reboot` (systemd-юнит в `deploy/sleep-diary.service` — требует sudo при установке)

## Перезапуск после обновления кода

```bash
pkill -f "python -m src.main"   # остановить текущий инстанс
cd /home/openclaw/child_bot
set -a && . ./.env && set +a
nohup /home/openclaw/venvs/sleepbot/bin/python -m src.main >> /home/openclaw/child_bot-data/bot.log 2>&1 &
```

## ОБЯЗАТЕЛЬНО: один инстанс

Telegram API разрешает только одного получателя getUpdates. Перед стартом —
`pgrep -f src.main` должен быть пуст. Два инстанса =выхватывают друг друга апдейтов и
«бот не отвечает» вперемешку.

## Живость бота

```bash
pgrep -f src.main                                  # процесс есть
tail -5 /home/openclaw/child_bot-data/bot.log      # без трейсбеков
curl -s "https://api.telegram.org/bot$BOT_TOKEN/getMe" | grep -o '"ok":true'
```

## Обновление зависимостей

```bash
/home/openclaw/venvs/sleepbot/bin/pip install -r requirements.txt
# затем перезапуск (см. выше); apscheduler уже доустановлен для этапа 2
```
