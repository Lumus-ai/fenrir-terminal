#!/usr/bin/env bash
# Fenrir Terminal — RAM watchdog v2.
# Пороги как в ram_guard(): OK ≥500 / WARN 300–499 / CRIT <300 МБ доступной RAM.
# Делегирует проверку и эскалацию fenrir_term.py guard (stdlib only):
#   OK   — молча, выход 0;
#   WARN — запись в logs/ram-guard.log + лёгкая чистка безопасного мусора;
#   CRIT — повторная проверка, запись в logs/ram-guard.log и
#          logs/ram-warnings.log + полная чистка (включая ротацию логов).
# Чистка удаляет ТОЛЬКО явный мусор (__pycache__, старые /tmp-очереди Lix,
# старые ротированные логи); serve, batch, runs/, state/, PLAN-EXTENDED.md,
# код терминала и файлы пользователя никогда не трогаются.
# --dry-run: только лог, ничего не удаляет. Выход всегда 0.
# Запуск вручную (fenrir watchdog) или по крону.
set -u
TERM_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$TERM_DIR/logs/ram-warnings.log"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi

if (( DRY_RUN )); then
    python3 "$TERM_DIR/fenrir_term.py" guard --dry-run
    PY_OK=$?
else
    python3 "$TERM_DIR/fenrir_term.py" guard
    PY_OK=$?
fi

# Fallback на уровне v1, если python-часть по какой-то причине не сработала:
# при < 300 МБ — строка в ram-warnings.log (формат v1).
if (( PY_OK != 0 )); then
    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null
    available_mb="$(free -m | awk '/^Mem:/ {print $7}')"
    if [[ "$available_mb" =~ ^[0-9]+$ ]] && (( available_mb < 300 )); then
        printf '%s Low RAM warning: available %s MB\n' \
            "$(date '+%Y-%m-%d %H:%M:%S')" "$available_mb" >> "$LOG_FILE" 2>/dev/null
    fi
fi

exit 0
