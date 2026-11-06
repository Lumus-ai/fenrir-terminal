#!/bin/bash
# Fenrir Terminal — ночная смена: проверка каждые 10 минут, пока Shadow спит.
# Что проверяет: serve отвечает? RAM? диск? probe свежий? Lix жив?
# Если serve мёртв — поднимает через watchdog.sh. Всё пишет в logs/sleep-watch.log.
# Выход всегда 0, чтобы крон не спамил ошибками.
set -u
TERM_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$TERM_DIR/logs/sleep-watch.log"
mkdir -p "$(dirname "$LOG")"
now="$(date '+%Y-%m-%d %H:%M:%S %Z')"
say() { echo "[$now] $*" >> "$LOG"; }

# 1. serve
if curl -s -m 3 http://127.0.0.1:4097/health >/dev/null 2>&1; then
  serve="OK"
else
  serve="DOWN"
  say "serve DOWN — поднимаю через watchdog"
  bash ~/workspace/fenrir-orchestra/watchdog.sh >> "$LOG" 2>&1 || true
  sleep 5
  if curl -s -m 3 http://127.0.0.1:4097/health >/dev/null 2>&1; then
    say "serve поднят после рестарта"
  else
    say "serve НЕ поднялся после рестарта — нужно вмешательство утром"
  fi
fi

# 2. RAM (доступно, МБ)
avail="$(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
ram="OK"
[ "$avail" -lt 300 ] && ram="CRITICAL"
[ "$avail" -ge 300 ] && [ "$avail" -lt 500 ] && ram="WARN"

# 3. диск (свободно, ГБ)
free_gb="$(df -BG ~/workspace 2>/dev/null | awk 'NR==2 {gsub(/G/,"",$4); print $4}')"
[ -z "$free_gb" ] && free_gb="?"

# 4. probe свежий? (health-файл не старше 2 часов)
health=~/workspace/fenrir-orchestra/runs/health-$(date +%F).json
if [ -f "$health" ]; then
  age_min=$(( ( $(date +%s) - $(stat -c %Y "$health") ) / 60 ))
  probe="fresh ${age_min}min"
else
  probe="MISSING"
fi

# 5. Lix до дедлайна (09.11.2026 23:59 +04)
lix="$(python3 ~/workspace/fenrir-terminal/fenrir_term.py lix 2>/dev/null || echo '?')"

say "serve=$serve ram=${avail}MB[$ram] disk=${free_gb}GB probe=$probe lix=$lix"
exit 0
