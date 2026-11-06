#!/usr/bin/env bash
# Fenrir Terminal v1.1 — RAM watchdog.
# Источник: ответ task-3 пачки прямых моделей Lix (qwen3.8-max),
# путь к логу привязан к каталогу терминала. Проверяет свободную RAM,
# если доступно < 300 МБ — пишет строку с датой в logs/ram-warnings.log.
# Выход всегда 0. Запуск вручную (fenrir watchdog) или по крону.
set -u
TERM_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$TERM_DIR/logs/ram-warnings.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null

available_mb="$(free -m | awk '/^Mem:/ {print $7}')"

if [[ "$available_mb" =~ ^[0-9]+$ ]] && (( available_mb < 300 )); then
    printf '%s Low RAM warning: available %s MB\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$available_mb" >> "$LOG_FILE" 2>/dev/null
fi

exit 0
