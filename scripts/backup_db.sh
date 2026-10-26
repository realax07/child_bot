#!/usr/bin/env bash
# Локальный бэкап БД бота «Дневник сна ребёнка».
# Change-пакет add-core-diary, задача 5.3 (FR-25, design D15, решение 26.10 №6).
#
# Бэкап — ТОЛЬКО локальный, рядом с файлом БД, через `sqlite3 .backup`
# (онлайн-снапшот: безопасен при journal_mode=WAL, в отличие от копирования
# файла). Никаких внешних вызовов, выгрузок в облако или сторонних сервисов —
# данные пользователей не покидают наш сервер.
#
# Использование:
#   scripts/backup_db.sh [путь-к-БД]
# Путь берётся из аргумента, иначе из env BOT_DB_PATH (как в src/db.py),
# иначе child_bot.db. Метка времени — в имени файла бэкапа.

set -euo pipefail

DB_PATH="${1:-${BOT_DB_PATH:-child_bot.db}}"

if [ ! -f "$DB_PATH" ]; then
    echo "ОШИБКА: файл БД не найден: $DB_PATH" >&2
    exit 1
fi

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_PATH="${DB_PATH}.backup-${TIMESTAMP}"

# .backup — штатный онлайн-бэкап SQLite (снапшот с учётом WAL), не file copy.
sqlite3 "$DB_PATH" ".backup '$BACKUP_PATH'"

if [ ! -s "$BACKUP_PATH" ]; then
    echo "ОШИБКА: бэкап не создан или пуст: $BACKUP_PATH" >&2
    exit 1
fi

echo "OK: $BACKUP_PATH"
