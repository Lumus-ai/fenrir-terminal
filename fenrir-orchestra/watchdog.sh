#!/bin/sh
# watchdog Fenrir-Orchestra: проверяет /health, при отказе перезапускает serve.
# Запуск вручную:  sh watchdog.sh
# В cron (каждые 5 минут):  */5 * * * * /home/hatch/workspace/fenrir-orchestra/watchdog.sh
# systemd --user в этой песочнице недоступен (нет bus) — проверено, поэтому cron.
DIR="/home/hatch/workspace/fenrir-orchestra"
LOG="$DIR/logs/watchdog.log"
mkdir -p "$DIR/logs"
if curl -fsS -m 5 http://127.0.0.1:4097/health >/dev/null 2>&1; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') ok" >> "$LOG"
  exit 0
fi
echo "$(date '+%Y-%m-%d %H:%M:%S') DOWN — restarting serve" >> "$LOG"
cd "$DIR" || exit 1
# setsid — обязательно: serve, запущенный из cron-агента, иначе умирает
# вместе с деревом процессов агента, когда run завершается (проверено
# 06.11.2026: перезапуск кроном в 01:06 поднял pid 23187, а к 01:07
# процесса уже не было). setsid отрывает serve в отдельную сессию.
setsid nohup python3 -u fenrir_orchestra.py serve >> "$DIR/logs/serve.log" 2>&1 < /dev/null &
echo "$(date '+%Y-%m-%d %H:%M:%S') restarted pid $!" >> "$LOG"
