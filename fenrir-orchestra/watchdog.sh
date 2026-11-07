#!/bin/sh
# watchdog Fenrir-Orchestra v2: проверяет /health, при отказе перезапускает serve.
# v2 (07.11.2026): экспоненциальный backoff между перезапусками, счётчик
# последовательных сбоев в state/ (переживает запуски скрипта), захват хвоста
# serve.log при падении, алерт после 3 сбоев подряд (маркер-файл в state/),
# graceful shutdown зависшего serve (SIGTERM, ожидание, SIGKILL).
# Запуск вручную:  sh watchdog.sh [--dry-run]
# В cron (каждые 5 минут):  */5 * * * * /home/hatch/workspace/fenrir-orchestra/watchdog.sh
# systemd --user в этой песочнице недоступен (нет bus) — проверено, поэтому cron.
# Для тестов: WATCHDOG_DIR, WATCHDOG_HEALTH_URL, WATCHDOG_SERVE_CMD,
# WATCHDOG_SERVE_MATCH, WATCHDOG_BASE_SLEEP, WATCHDOG_MAX_SLEEP,
# WATCHDOG_GRACE_TIMEOUT переопределяют значения по умолчанию.
DIR="${WATCHDOG_DIR:-/home/hatch/workspace/fenrir-orchestra}"
LOG="$DIR/logs/watchdog.log"
STATE_DIR="$DIR/state"
FAIL_FILE="$STATE_DIR/watchdog-failures"
ALERT_FILE="$STATE_DIR/watchdog-alert"
PID_FILE="$STATE_DIR/serve.pid"
CRASH_DIR="$DIR/logs/crashes"
HEALTH_URL="${WATCHDOG_HEALTH_URL:-http://127.0.0.1:4097/health}"
SERVE_CMD="${WATCHDOG_SERVE_CMD:-python3 -u fenrir_orchestra.py serve}"
# ERE-паттерн для опознания процесса serve (якорь ^ — чтобы pgrep не ловил
# оболочки, в чьей командной строке строка встречается как подстрока).
SERVE_MATCH="${WATCHDOG_SERVE_MATCH:-^python3 -u fenrir_orchestra.py serve}"
BASE_SLEEP="${WATCHDOG_BASE_SLEEP:-30}"
MAX_SLEEP="${WATCHDOG_MAX_SLEEP:-600}"
ALERT_AFTER=3
GRACE_TIMEOUT="${WATCHDOG_GRACE_TIMEOUT:-10}"
CRASH_TAIL_LINES=100
CRASH_KEEP=10

DRY_RUN=0
[ "$1" = "--dry-run" ] && DRY_RUN=1
[ "$WATCHDOG_DRY_RUN" = "1" ] && DRY_RUN=1

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() {
  if [ "$DRY_RUN" = 1 ]; then
    echo "$(ts) [dry-run] $*" >> "$LOG"
    echo "dry-run: $*"
  else
    echo "$(ts) $*" >> "$LOG"
  fi
}

mkdir -p "$DIR/logs" "$STATE_DIR" "$CRASH_DIR"

# --- health OK: сброс счётчика сбоев и снятие алерта ---
if curl -fsS -m 5 "$HEALTH_URL" >/dev/null 2>&1; then
  if [ -f "$FAIL_FILE" ]; then
    fails=$(cut -d' ' -f1 "$FAIL_FILE" 2>/dev/null)
    case "$fails" in ''|*[!0-9]*) fails=0 ;; esac
    rm -f "$FAIL_FILE"
    log "ok (recovered after $fails failures)"
  else
    log "ok"
  fi
  if [ -f "$ALERT_FILE" ]; then
    rm -f "$ALERT_FILE"
    log "alert cleared"
  fi
  exit 0
fi

# --- serve DOWN: счётчик и backoff ---
now=$(date +%s)
fails=0
last=0
if [ -f "$FAIL_FILE" ]; then
  read fails last < "$FAIL_FILE" 2>/dev/null
  case "$fails" in ''|*[!0-9]*) fails=0 ;; esac
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
fi
fails=$((fails + 1))

# backoff = min(BASE * 2^(fails-1), MAX): 30, 60, 120, 240, 480, 600...
backoff=$BASE_SLEEP
i=1
while [ "$i" -lt "$fails" ]; do
  backoff=$((backoff * 2))
  if [ "$backoff" -ge "$MAX_SLEEP" ]; then backoff=$MAX_SLEEP; break; fi
  i=$((i + 1))
done

# --- алерт после 3 последовательных сбоев (один раз за серию) ---
if [ "$fails" -ge "$ALERT_AFTER" ] && [ ! -f "$ALERT_FILE" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    log "would raise alert (serve down $fails times in a row)"
  else
    echo "$(ts) serve down $fails times in a row (health: $HEALTH_URL)" > "$ALERT_FILE"
    log "ALERT: serve down $fails times in a row — marker $ALERT_FILE"
  fi
fi

# --- backoff: если с прошлой попытки прошло меньше задержки — пропустить рестарт ---
elapsed=$((now - last))
if [ "$last" -gt 0 ] && [ "$elapsed" -lt "$backoff" ]; then
  [ "$DRY_RUN" = 0 ] && echo "$fails $last" > "$FAIL_FILE"
  log "DOWN fail #$fails — backoff ${backoff}s, skip restart, retry in $((backoff - elapsed))s"
  exit 0
fi

# --- захват хвоста serve.log при падении ---
if [ -f "$DIR/logs/serve.log" ]; then
  crash="$CRASH_DIR/crash-$(date '+%Y%m%d_%H%M%S').log"
  if [ "$DRY_RUN" = 1 ]; then
    log "would save crash tail to $crash"
  else
    tail -n "$CRASH_TAIL_LINES" "$DIR/logs/serve.log" > "$crash" 2>/dev/null
    log "DOWN fail #$fails — crash tail saved to $crash"
    # ротация: оставить последние CRASH_KEEP дампов
    ls -t "$CRASH_DIR"/crash-*.log 2>/dev/null | tail -n +$((CRASH_KEEP + 1)) | while read f; do rm -f "$f"; done
  fi
fi

# --- graceful shutdown зависшего serve, если процесс ещё жив ---
serve_pid() {
  pid=""
  if [ -f "$PID_FILE" ]; then
    pid=$(cat "$PID_FILE" 2>/dev/null)
    case "$pid" in ''|*[!0-9]*) pid="" ;; esac
  fi
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    # не убивать чужой pid: проверить cmdline
    if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qE "$SERVE_MATCH"; then
      echo "$pid"
      return 0
    fi
    pid=""
  fi
  pgrep -f "$SERVE_MATCH" 2>/dev/null | grep -v "^$$\$" | head -n 1
}

stop_serve() {
  pid=$(serve_pid)
  [ -z "$pid" ] && return 0
  if [ "$DRY_RUN" = 1 ]; then
    log "would SIGTERM hung serve pid $pid"
    return 0
  fi
  log "stopping hung serve pid $pid (SIGTERM, grace ${GRACE_TIMEOUT}s)"
  kill -TERM "$pid" 2>/dev/null
  waited=0
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$GRACE_TIMEOUT" ]; do
    sleep 1
    waited=$((waited + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    log "serve pid $pid did not stop in ${GRACE_TIMEOUT}s — SIGKILL"
    kill -KILL "$pid" 2>/dev/null
    sleep 1
  fi
  rm -f "$PID_FILE"
}

# --- перезапуск ---
log "DOWN fail #$fails — restarting serve"
cd "$DIR" || exit 1
stop_serve
if [ "$DRY_RUN" = 1 ]; then
  log "would start serve"
else
  # setsid — обязательно: serve, запущенный из cron-агента, иначе умирает
  # вместе с деревом процессов агента, когда run завершается (проверено
  # 06.11.2026: перезапуск кроном в 01:06 поднял pid 23187, а к 01:07
  # процесса уже не было). setsid отрывает serve в отдельную сессию.
  setsid nohup $SERVE_CMD >> "$DIR/logs/serve.log" 2>&1 < /dev/null &
  newpid=$!
  echo "$newpid" > "$PID_FILE"
  log "restarted pid $newpid"
  sleep 3
  if curl -fsS -m 5 "$HEALTH_URL" >/dev/null 2>&1; then
    log "serve back up (pid $newpid)"
  else
    log "serve still down after restart (pid $newpid)"
  fi
  echo "$fails $now" > "$FAIL_FILE"
fi
