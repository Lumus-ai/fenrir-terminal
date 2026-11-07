#!/usr/bin/env python3
"""Fenrir Terminal v3.2 — пульт с телефона, ШАГ 1 (stdlib only).

Telegram-бот как пульт: статус, лимиты, задачи, дайджест; опасные
действия — только после подтверждения; уведомления — одна строка.

Что в шаге 1:
- Парсер команд пульта: status, limits, tasks, digest, help, history,
  pending — чтение, без подтверждения; stop, start, restart —
  опасные, только после подтверждения.
- Подтверждение — двухшаговое (по основному плану v3.2, раздел 9):
  команда создаёт запрос с id, действует 5 минут, затем авто-отклон.
  Подтвердить — `confirm <id>`, отклонить — `reject <id>`.
  Вариант из сгенерированной спеки (4111) — 6-значный код на 2 минуты —
  НЕ взят: id в инлайн-кнопке удобнее и не требует перепечатки кода.
- Whitelist user_id + rate limit 1 команда в 30 секунд на пользователя
  (confirm/reject от лимита освобождены — они завершают уже начатый
  поток, иначе подтвердить в течение 30 секунд было бы нельзя).
- Журнал аудита logs/remote-audit.log: время, пользователь, команда,
  результат; `history` — последние 20 строк за 7 дней.
- Уведомления: одна строка `🐺 <событие>: <текст>` (готово/упало/алерт),
  отправка через коннектор custom.telegram-bot, только в настроенный
  chat_id. Без chat_id — dry-run (только лог, ничего не отправляется).
- Polling-цикл `listen`: long-poll getUpdates, диспетчеризация через
  handle(), ответы через sendMessage. Требует настроенного
  state/remote-config.json, иначе выходит с объяснением.

Что НЕ в шаге 1 (отдельные шаги): inline-клавиатуры Telegram,
команды /pause, /resume, /add, /logs, /diff, /approve, /reject,
/retry, /stats из спеки; webhook вместо polling.

ВАЖНО — бот и доступ:
- За коннектором custom.telegram-bot стоит @Postingmog_bot
  (id 8905015348) — бот постинга канала «Нейросигнал», НЕ отдельный
  бот-пульт. getUpdates пуст — Shadow боту в личку не писал, поэтому
  его chat_id неизвестен.
- Для живого пульта нужно от Shadow ОДНО из двух:
  1. Отдельный бот для пульта: BotFather → /newbot → токен через
     защищённую форму (новый коннектор, токен поста не трогать); или
  2. Разрешение использовать @Postingmog_bot как пульт + Shadow пишет
     боту /start в личку — тогда chat_id читается из getUpdates.
- После этого: `fenrir remote allow <user_id>` (локально, из shell)
  и chat_id в state/remote-config.json. Без этого пульт отвечает
  только локально через CLI, а listen отказывается запускаться.
- В канал «Нейросигнал» пульт НЕ пишет никогда: уведомления идут
  только в личный чат из конфига.

Токены — только через коннектор (суррогат), никуда не записываются.
Опасные действия выполняются только после подтверждения; команды
чтения — без подтверждения. Модуль никогда не отправляет в Telegram
то, что не просили: notify вызывается только явно или из других
модулей терминала при событии (готово/упало).

CLI (через bin/fenrir):
  fenrir remote status | limits | tasks | digest | help | history | pending
  fenrir remote stop | start | restart   (создаёт запрос на подтверждение)
  fenrir remote confirm <id> | reject <id>
  fenrir remote notify <событие> <текст> [--dry-run]
  fenrir remote allow <user_id> | config
  fenrir remote listen [--timeout N]

Запуск тестов: python3 test_remote.py
"""
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from fenrir_worklog import worklog

try:
    from zoneinfo import ZoneInfo
    SAMARA = ZoneInfo("Europe/Samara")
except Exception:  # pragma: no cover - нет tzdata
    SAMARA = timezone(timedelta(hours=4))

TERM_DIR = os.path.dirname(os.path.abspath(__file__))
ORCH_DIR = os.path.join(os.path.expanduser("~"), "workspace", "fenrir-orchestra")
STATE_DIR = os.path.join(TERM_DIR, "state")
LOGS_DIR = os.path.join(TERM_DIR, "logs")
CONFIG_PATH = os.path.join(STATE_DIR, "remote-config.json")
PENDING_PATH = os.path.join(STATE_DIR, "remote-pending.json")
RATELIMIT_PATH = os.path.join(STATE_DIR, "remote-ratelimit.json")
OFFSET_PATH = os.path.join(STATE_DIR, "remote-offset.txt")
AUDIT_LOG = os.path.join(LOGS_DIR, "remote-audit.log")
TELEGRAM_HELPER = os.path.join(
    os.path.expanduser("~"), "workspace", "custom-connectors",
    "custom.telegram-bot", "bin", "telegram.py")

PENDING_TTL_S = 300          # подтверждение действует 5 минут
RATE_LIMIT_S = 30            # максимум 1 команда в 30 секунд
HISTORY_DAYS = 7
HISTORY_LINES = 20

READ_COMMANDS = ("status", "limits", "tasks", "queue", "digest",
                 "help", "history", "pending")
DANGEROUS_COMMANDS = ("stop", "start", "restart")
CONFIRM_COMMANDS = ("confirm", "reject")
NOTIFY_EVENTS = ("готово", "упало", "алерт")

LOCAL_USER = "local"  # CLI из shell песочницы — доверенный локальный вызов


# ------------------------------------------------------------------ утилиты

def _now():
    return datetime.now(SAMARA)


def _ts(dt=None):
    return (dt or _now()).strftime("%Y-%m-%d %H:%M:%S")


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def _save_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def _audit(user, command, result):
    os.makedirs(LOGS_DIR, exist_ok=True)
    line = f"{_ts()} | user={user} | {command} | {result}\n"
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


# ------------------------------------------------------------------ конфиг

def load_config():
    cfg = _load_json(CONFIG_PATH, {})
    return {
        "chat_id": cfg.get("chat_id"),
        "allowed_users": list(cfg.get("allowed_users") or []),
        "notifications": bool(cfg.get("notifications", True)),
    }


def save_config(cfg):
    _save_json_atomic(CONFIG_PATH, {
        "chat_id": cfg.get("chat_id"),
        "allowed_users": list(cfg.get("allowed_users") or []),
        "notifications": bool(cfg.get("notifications", True)),
    })


def add_allowed_user(user_id):
    cfg = load_config()
    if user_id not in cfg["allowed_users"]:
        cfg["allowed_users"].append(user_id)
        save_config(cfg)
        return True
    return False


# ------------------------------------------------------- подтверждения (pending)

def _load_pending():
    return _load_json(PENDING_PATH, {})


def _save_pending(data):
    _save_json_atomic(PENDING_PATH, data)


def _prune_expired(pending, now_epoch=None):
    now_epoch = now_epoch if now_epoch is not None else time.time()
    alive = {}
    for pid, req in pending.items():
        if isinstance(req, dict) and req.get("expires", 0) > now_epoch:
            alive[pid] = req
        else:
            _audit(req.get("user", "?") if isinstance(req, dict) else "?",
                   f"{req.get('command', '?')}" if isinstance(req, dict) else "?",
                   "авто-отклон: подтверждение устарело")
    if len(alive) != len(pending):
        _save_pending(alive)
    return alive


def create_pending(user, command, args):
    pending = _prune_expired(_load_pending())
    pid = secrets.token_hex(4)
    now = time.time()
    pending[pid] = {
        "user": user, "command": command, "args": list(args),
        "created": now, "expires": now + PENDING_TTL_S,
    }
    _save_pending(pending)
    return pid


def consume_pending(pid, user):
    """Забрать запрос на подтверждение. Возвращает (req, error)."""
    pending = _prune_expired(_load_pending())
    req = pending.get(pid)
    if req is None:
        return None, "Неизвестный id подтверждения."
    if str(req.get("user")) != str(user):
        return None, "Это подтверждение не ваше."
    del pending[pid]
    _save_pending(pending)
    return req, None


def reject_pending(pid, user):
    req, err = consume_pending(pid, user)
    if err:
        return err
    return f"Отменено: {req['command']} не выполнено."


# ------------------------------------------------------------- rate limit

def _check_rate_limit(user):
    if str(user) == LOCAL_USER:
        return None
    data = _load_json(RATELIMIT_PATH, {})
    last = data.get(str(user), 0)
    now = time.time()
    if now - last < RATE_LIMIT_S:
        wait = int(RATE_LIMIT_S - (now - last))
        return (f"Слишком часто: подождите {wait} сек. "
                f"(максимум 1 команда в 30 секунд).")
    data[str(user)] = now
    _save_json_atomic(RATELIMIT_PATH, data)
    return None


# ------------------------------------------------------------ чтение данных

def _serve_health():
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:4097/health", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        uptime = data.get("uptime_s")
        if uptime is not None:
            up = f", uptime {int(uptime) // 60} мин"
        else:
            up = ""
        return f"serve OK{up}"
    except Exception:
        return "serve НЕ отвечает"


def cmd_status():
    from fenrir_term import lix_countdown, ram_guard
    mb, st = ram_guard()
    lines = [
        "🐺 Фенрир Терминал — статус",
        f"serve: {_serve_health()}",
        f"RAM доступно: {mb} МБ [{st}]",
        lix_countdown(),
    ]
    return "\n".join(lines)


def cmd_limits():
    from fenrir_term import lix_countdown, ram_guard
    try:
        from fenrir_term import klinch_limits
        rows = klinch_limits()
    except Exception as e:  # pragma: no cover - защита от падения probe
        rows = [f"Klinch: ошибка чтения лимитов ({type(e).__name__})"]
    mb, st = ram_guard()
    lines = ["🐺 Лимиты", f"RAM доступно: {mb} МБ [{st}]",
             f"Lix: {lix_countdown()}"] + rows
    return "\n".join(lines)


def cmd_tasks():
    from fenrir_scheduler import load_tasks
    data, errors = load_tasks()
    if data is None:
        return "⚠ tasks.yaml — ошибка:\n" + "\n".join(f"  - {e}" for e in errors)
    tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]
    if not tasks:
        return "Задач нет."
    lines = ["📋 Задачи:"]
    for t in tasks:
        lines.append(f"  - {t.get('id')}: {t.get('title')} "
                     f"[{t.get('status')}, {t.get('schedule')}]")
    if errors:
        lines.append("⚠ Ошибки валидации:")
        lines.extend(f"  - {e}" for e in errors)
    return "\n".join(lines)


def cmd_digest():
    from fenrir_digest import collect_stats, render_digest
    return render_digest(collect_stats())


def cmd_history():
    try:
        with open(AUDIT_LOG, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return "Журнал аудита пуст."
    cutoff = (_now() - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    recent = [ln for ln in lines if ln[:10] >= cutoff]
    if not recent:
        return f"За последние {HISTORY_DAYS} дней команд не было."
    return "🕐 История команд:\n" + "\n".join(recent[-HISTORY_LINES:])


def cmd_pending(user):
    pending = _prune_expired(_load_pending())
    mine = {pid: r for pid, r in pending.items()
            if str(r.get("user")) == str(user)}
    if not mine:
        return "Активных запросов на подтверждение нет."
    lines = ["⏳ Ожидают подтверждения:"]
    for pid, r in mine.items():
        left = max(0, int(r["expires"] - time.time()))
        lines.append(f"  - {pid}: {r['command']} (осталось {left} сек)")
    return "\n".join(lines)


HELP_TEXT = (
    "🐺 Пульт Фенрира — команды:\n"
    "/status — статус терминала\n"
    "/limits — лимиты и RAM\n"
    "/tasks — задачи планировщика\n"
    "/digest — утренний дайджест\n"
    "/history — история команд за 7 дней\n"
    "/stop, /start, /restart — опасные, только после подтверждения\n"
    "/pending — активные запросы на подтверждение\n"
    "Опасные действия: команда → /confirm <id> в течение 5 минут."
)


# ---------------------------------------------------------- опасные действия

def default_executor(command, args):
    """Выполнить подтверждённое опасное действие. Только после confirm."""
    if command == "stop":
        proc = subprocess.run(
            ["pkill", "-f", "fenrir_orchestra.py serv[e]"],
            capture_output=True, text=True, timeout=30)
        return ("serve остановлен." if proc.returncode == 0
                else "serve не работал.")
    if command == "start":
        subprocess.run(["bash", os.path.join(ORCH_DIR, "watchdog.sh")],
                       capture_output=True, text=True, timeout=60)
        time.sleep(2)
        health = _serve_health()
        return f"serve запущен: {health}."
    if command == "restart":
        default_executor("stop", args)
        time.sleep(2)
        return default_executor("start", args)
    return f"Неизвестное действие: {command}"


# ------------------------------------------------------------------ парсер

def parse_command(text):
    """Разобрать текст команды. Возвращает (cmd, args) или (None, error)."""
    if not text or not text.strip():
        return None, "Пустая команда. Смотрите /help."
    parts = text.strip().split()
    cmd = parts[0].lstrip("/").lower()
    args = parts[1:]
    if cmd in READ_COMMANDS or cmd in DANGEROUS_COMMANDS \
            or cmd in CONFIRM_COMMANDS or cmd == "notify":
        return cmd, args
    return None, f"Неизвестная команда: {parts[0]}. Смотрите /help."


# -------------------------------------------------------------- диспетчер

def handle(user_id, text, executor=None):
    """Обработать одну команду пульта. Возвращает текст ответа.

    executor(command, args) — выполнение опасных действий (по
    умолчанию default_executor); в тестах подменяется стабом.
    """
    executor = executor or default_executor
    user = str(user_id)
    cfg = load_config()

    # 1. Whitelist: без настроенного списка — всем отказ.
    if user != LOCAL_USER:
        if not cfg["allowed_users"]:
            _audit(user, text, "отказ: пульт не настроен (нет allowed_users)")
            return ("⛔ Пульт не настроен: список разрешённых пользователей "
                    "пуст. Обратитесь к владельцу терминала.")
        if user_id not in cfg["allowed_users"] \
                and user not in [str(u) for u in cfg["allowed_users"]]:
            _audit(user, text, "отказ: нет в whitelist")
            return "⛔ Нет доступа к пульту."

    # 2. Парсинг (до rate limit, чтобы мусор не тратил лимит).
    cmd, args_or_err = parse_command(text)
    if cmd is None:
        _audit(user, text, f"отказ: {args_or_err}")
        return args_or_err
    args = args_or_err

    # 3. Rate limit (confirm/reject — вне лимита, см. докстринг).
    if cmd not in CONFIRM_COMMANDS:
        limited = _check_rate_limit(user)
        if limited:
            _audit(user, cmd, f"отказ: {limited}")
            return limited

    # 4. Подтверждение / отклонение.
    if cmd == "confirm":
        if not args:
            return "Укажите id: /confirm <id>."
        req, err = consume_pending(args[0], user)
        if err:
            _audit(user, f"confirm {args[0]}", f"отказ: {err}")
            return err
        try:
            result = executor(req["command"], req.get("args", []))
        except Exception as e:  # pragma: no cover - защита исполнителя
            result = f"ошибка выполнения: {type(e).__name__}: {e}"
        _audit(user, req["command"], f"выполнено после подтверждения: {result}")
        worklog("remote", req["command"], "ok", result)
        return f"✅ Выполнено: {req['command']} — {result}"
    if cmd == "reject":
        if not args:
            return "Укажите id: /reject <id>."
        result = reject_pending(args[0], user)
        _audit(user, f"reject {args[0]}", result)
        return result

    # 5. Опасные команды — только запрос на подтверждение.
    if cmd in DANGEROUS_COMMANDS:
        pid = create_pending(user, cmd, args)
        _audit(user, cmd, f"запрос подтверждения {pid}")
        return (f"⚠️ Подтвердите действие: {cmd} терминала.\n"
                f"Введите: /confirm {pid} (действует 5 минут).\n"
                f"Отмена: /reject {pid}.")

    # 6. Чтение.
    try:
        if cmd == "status":
            reply = cmd_status()
        elif cmd == "limits":
            reply = cmd_limits()
        elif cmd in ("tasks", "queue"):
            reply = cmd_tasks()
        elif cmd == "digest":
            reply = cmd_digest()
        elif cmd == "help":
            reply = HELP_TEXT
        elif cmd == "history":
            reply = cmd_history()
        elif cmd == "pending":
            reply = cmd_pending(user)
        else:  # pragma: no cover - парсер не пропускает такое
            reply = HELP_TEXT
    except Exception as e:  # pragma: no cover - защита команд чтения
        reply = f"Ошибка команды {cmd}: {type(e).__name__}: {e}"
    _audit(user, cmd, "ok")
    return reply


# ------------------------------------------------------------- уведомления

def _telegram_send(chat_id, text):
    sys.path.insert(0, os.path.dirname(TELEGRAM_HELPER))
    import telegram as tg
    return tg.api_call("sendMessage", {"chat_id": chat_id, "text": text})


def notify(event, text, dry_run=False, sender=None):
    """Одна строка в Telegram: `🐺 <событие>: <текст>`.

    Возвращает (sent: bool, line). Без настроенного chat_id или при
    dry_run — ничего не отправляется, только запись в аудит.
    """
    if event not in NOTIFY_EVENTS:
        return False, f"Неизвестное событие: {event} (готово/упало/алерт)."
    line = f"🐺 {event}: {text}".replace("\n", " ")
    cfg = load_config()
    if dry_run or not cfg["chat_id"] or not cfg["notifications"]:
        _audit(LOCAL_USER, f"notify {event}", f"dry-run: {line}")
        return False, f"[dry-run] {line}"
    send = sender or _telegram_send
    try:
        send(cfg["chat_id"], line)
    except Exception as e:
        _audit(LOCAL_USER, f"notify {event}", f"ошибка отправки: {e}")
        return False, f"Ошибка отправки уведомления: {e}"
    _audit(LOCAL_USER, f"notify {event}", f"отправлено: {line}")
    worklog("remote", f"notify {event}", "ok", line)
    return True, line


# ------------------------------------------------------------ polling-цикл

def _telegram_get_updates(offset, timeout=30):
    sys.path.insert(0, os.path.dirname(TELEGRAM_HELPER))
    import telegram as tg
    params = {"timeout": timeout, "limit": 10}
    if offset is not None:
        params["offset"] = offset
    return tg.api_call("getUpdates", params)


def listen(timeout=None, get_updates=None, sender=None):
    """Цикл опроса бота. Требует настроенного конфига.

    timeout — секунды работы (None = бесконечно, до Ctrl-C).
    Возвращает число обработанных сообщений.
    """
    cfg = load_config()
    if not cfg["chat_id"] or not cfg["allowed_users"]:
        print("Пульт не настроен: нужен chat_id и allowed_users в "
              "state/remote-config.json.")
        print("Что нужно от Shadow: отдельный бот для пульта ИЛИ "
              "разрешение использовать @Postingmog_bot + /start боту "
              "в личку, затем `fenrir remote allow <user_id>`.")
        return 0
    offset = None
    try:
        with open(OFFSET_PATH, encoding="utf-8") as f:
            offset = int(f.read().strip())
    except (OSError, ValueError):
        offset = None
    get_updates = get_updates or _telegram_get_updates
    handled = 0
    started = time.time()
    print(f"Пульт слушает Telegram (chat {cfg['chat_id']})...")
    try:
        while True:
            if timeout is not None and time.time() - started >= timeout:
                break
            try:
                data = get_updates(offset, timeout=5)
            except Exception as e:
                print(f"getUpdates: {e}")
                time.sleep(5)
                continue
            for upd in (data.get("result") or []):
                offset = upd.get("update_id", 0) + 1
                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                sender_fn = sender or _telegram_send
                reply = handle(msg.get("from", {}).get("id"), msg.get("text", ""))
                try:
                    sender_fn(chat.get("id"), reply)
                except Exception as e:
                    print(f"sendMessage: {e}")
                handled += 1
            if offset is not None:
                with open(OFFSET_PATH, "w", encoding="utf-8") as f:
                    f.write(str(offset))
            if timeout is not None and time.time() - started >= timeout:
                break
    except KeyboardInterrupt:
        print("Пульт остановлен.")
    return handled


# --------------------------------------------------------------------- CLI

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(HELP_TEXT)
        return 0
    cmd = argv[0].lstrip("/").lower()
    args = argv[1:]

    if cmd == "allow":
        if not args:
            print("Укажите user_id: fenrir remote allow <user_id>")
            return 1
        try:
            uid = int(args[0])
        except ValueError:
            print("user_id должен быть числом.")
            return 1
        added = add_allowed_user(uid)
        print(f"user {uid} {'добавлен в whitelist' if added else 'уже в списке'}.")
        return 0
    if cmd == "config":
        cfg = load_config()
        print(f"chat_id: {cfg['chat_id'] or 'НЕ НАСТРОЕН'}")
        print(f"allowed_users: {cfg['allowed_users'] or 'ПУСТО'}")
        print(f"notifications: {'on' if cfg['notifications'] else 'off'}")
        print("Бот за коннектором: @Postingmog_bot (id 8905015348) — "
              "бот постинга канала, НЕ отдельный бот-пульт.")
        return 0
    if cmd == "notify":
        if len(args) < 2:
            print("Использование: fenrir remote notify <готово|упало|алерт> "
                  "<текст> [--dry-run]")
            return 1
        dry_run = "--dry-run" in args
        args = [a for a in args if a != "--dry-run"]
        sent, line = notify(args[0], " ".join(args[1:]), dry_run=dry_run)
        print(("Отправлено: " if sent else "") + line)
        return 0
    if cmd == "listen":
        timeout = None
        if "--timeout" in args:
            i = args.index("--timeout")
            try:
                timeout = int(args[i + 1])
            except (IndexError, ValueError):
                print("--timeout требует число секунд.")
                return 1
        return listen(timeout=timeout)

    # Все остальные команды — через общий диспетчер (локальный юзер).
    reply = handle(LOCAL_USER, " ".join([cmd] + args))
    print(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
