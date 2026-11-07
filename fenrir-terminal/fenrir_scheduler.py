#!/usr/bin/env python3
"""Fenrir Terminal v1.4 — ядро планировщика (stdlib only).

Что это: единый tasks.yaml (один источник правды), CLI
`fen task add/done/list/run/validate` и runner, который по cron
(раз в 10 мин, крон ставит человек — см. инструкцию внизу) находит
задачи к выполнению, запускает их через fenrir_agent.run_task и
пишет результаты в logs/scheduler.log, помечая задачи done/failed.

Запуск runner'а (крон, ставит человек, отдельный крон НЕ создаётся):
    */10 * * * * /home/hatch/workspace/fenrir-terminal/bin/fenrir scheduler --once

Провайдер по умолчанию — Klinch (приказ Shadow 06.11.2026 ~21:30:
планировщик v1.4 использует Klinch; это ЕДИНСТВЕННОЕ исключение
из правила «только Lix»). Дефолт агента без указания провайдера —
по-прежнему Lix; здесь провайдер берётся из задачи (поле provider,
по умолчанию klinch).

Безопасность (ядро, не спека целиком):
- Задачи выполняются ТОЛЬКО через fenrir_agent.run_task — агент
  пишет файлы только внутри workdir задачи (safe_path). Прямого
  запуска shell-команд, скриптов и HTTP-запросов из tasks.yaml
  НЕТ: типы command/script/http_request из спеки (пачка 2) в ядро
  НЕ вошли сознательно — это произвольный код по файлу, который
  правится руками. Если понадобится — только отдельным решением.
- Runner берёт только задачи со статусом todo и наступившим
  временем (due или cron-окно); ручные правки tasks.yaml ловит
  валидация (строгая схема, неизвестные ключи — ошибка).
- workdir задачи обязан лежать внутри ~/workspace (иначе задача
  отклоняется валидацией). Никаких авто-действий вне workdir.
- Запись tasks.yaml — атомарная (tmp + os.replace), после каждой
  выполненной задачи. Параллельный запуск runner'а блокируется
  lock-файлом logs/scheduler.lock (битый старше 10 мин пересоздаётся).

Формат tasks.yaml — YAML-подмножество (stdlib: парсера YAML нет).
Выбор: минимальный СТРОГИЙ парсер под задокументированную схему
(см. parse_yaml_subset / dump_tasks_yaml), а не смена формата:
файл называется tasks.yaml и выглядит как YAML, спека требует
YAML, а JSON с расширением .yaml вводил бы в заблуждение.
Парсер поддерживает ровно то, что пишет dumper: отображения,
списки отображений, скаляры (кавычки, plain, int, bool, null),
flow-списки [a, b], комментарии. Всё остальное (|, >, &, *,
{...}, якоря, табы) — ошибка с номером строки.

Схема задачи (все поля — плоские, вложенных params нет):
    id: FNR-001            # [A-Za-z0-9][A-Za-z0-9_-]*, уникальный
    title: "Имя"           # обязательно
    task: "Текст задачи для агента"   # обязательно
    workdir: "~/workspace/..."       # обязательно, внутри ~/workspace
    status: todo           # todo | done | failed
    kind: code             # code | text (по умолчанию code)
    provider: klinch       # klinch | lix (по умолчанию klinch)
    schedule: manual       # manual | cron "*/10 * * * *" (5 полей)
    due: 2026-11-07T15:30:00+04:00   # опционально, ISO
    max_steps: 15          # опционально, 1..100, по умолчанию 15
    priority: P1           # опционально, P0..P3
    created: ...           # ISO, ставится автоматически
    last_run: ...          # ISO, ставит runner
    last_result: "ok"      # ставит runner (ok/failed + коротко)

Правила срабатывания (is_due):
- статус != todo — никогда не выполняется;
- due задан и due <= now — выполняется (разовая задача);
- due в будущем — не выполняется;
- schedule — cron, due нет — выполняется, если cron совпал в
  окне (now - 10 мин, now] и last_run вне этого окна (защита от
  повторного запуска в том же окне);
- schedule manual без due — никогда автоматически (только
  `fen task run <id>`).
После выполнения: cron-задача остаётся todo (рекуррентная,
обновляется last_run), разовая — done/failed.
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    SAMARA = ZoneInfo("Europe/Samara")
except Exception:
    SAMARA = timezone(timedelta(hours=4))

TERM_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_PATH = os.path.join(TERM_DIR, "tasks.yaml")
LOGS_DIR = os.path.join(TERM_DIR, "logs")
SCHEDULER_LOG = os.path.join(LOGS_DIR, "scheduler.log")
SCHEDULER_LOCK = os.path.join(LOGS_DIR, "scheduler.lock")
LOCK_STALE_S = 600
LOG_MAX_BYTES = 1_048_576

SCHEMA_VERSION = "1.4"
TASK_STATUSES = ("todo", "done", "failed")
TASK_KINDS = ("code", "text")
TASK_PROVIDERS = ("klinch", "lix")
TASK_PRIORITIES = ("P0", "P1", "P2", "P3")
TASK_FIELDS = {
    "id", "title", "task", "workdir", "status", "kind", "provider",
    "schedule", "due", "max_steps", "priority", "created",
    "last_run", "last_result",
}
TASK_REQUIRED = ("id", "title", "task", "workdir", "status")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
DEFAULT_MAX_STEPS = 15
DEFAULT_WINDOW_MIN = 10


class YamlError(ValueError):
    pass


class ValidationError(ValueError):
    pass


# ---------------------------------------------------------------- YAML-парсер
def _strip_comment(line):
    """Убирает хвостовой комментарий '# ...' вне кавычек."""
    in_s = in_d = False
    i = 0
    while i < len(line):
        c = line[i]
        if in_d:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_d = False
        elif in_s:
            if c == "'":
                if i + 1 < len(line) and line[i + 1] == "'":
                    i += 2
                    continue
                in_s = False
        else:
            if c == '"':
                in_d = True
            elif c == "'":
                in_s = True
            elif c == "#" and (i == 0 or line[i - 1] in " \t"):
                return line[:i].rstrip()
        i += 1
    return line.rstrip()


def _split_flow(inner):
    """Разбивает содержимое [...] на элементы, уважая кавычки."""
    parts, cur = [], ""
    in_s = in_d = False
    i = 0
    while i < len(inner):
        c = inner[i]
        if in_d:
            cur += c
            if c == "\\" and i + 1 < len(inner):
                cur += inner[i + 1]
                i += 2
                continue
            if c == '"':
                in_d = False
        elif in_s:
            cur += c
            if c == "'":
                in_s = False
        else:
            if c == '"':
                in_d = True
                cur += c
            elif c == "'":
                in_s = True
                cur += c
            elif c == ",":
                parts.append(cur)
                cur = ""
            else:
                cur += c
        i += 1
    if cur.strip() or parts:
        parts.append(cur)
    return parts


def _parse_scalar(text, lineno):
    s = text.strip()
    if not s:
        return None
    if s.startswith('"'):
        if len(s) < 2 or not s.endswith('"'):
            raise YamlError(f"строка {lineno}: незакрытая кавычка: {s!r}")
        body = s[1:-1]
        # Ручной unescape только \\, \", \n, \t, \r — без unicode_escape,
        # который ломает кириллицу (он разбирает UTF-8-байты как latin-1).
        out, i = [], 0
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r",
                            '"': '"', "\\": "\\"}.get(nxt, "\\" + nxt))
                i += 2
            else:
                out.append(c)
                i += 1
        return "".join(out)
    if s.startswith("'"):
        if len(s) < 2 or not s.endswith("'"):
            raise YamlError(f"строка {lineno}: незакрытая кавычка: {s!r}")
        return s[1:-1].replace("''", "'")
    if s[0] in "|>&*!{":
        raise YamlError(f"строка {lineno}: не поддерживается в подмножестве: {s!r}")
    if s.startswith("["):
        if not s.endswith("]"):
            raise YamlError(f"строка {lineno}: незакрытый список: {s!r}")
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(p, lineno) for p in _split_flow(inner)]
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if s in ("null", "Null", "NULL", "~"):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    if ": " in s or s.endswith(":") or s.startswith("- "):
        raise YamlError(f"строка {lineno}: plain-скаляр с ':' или '- ': {s!r}")
    return s


_KEY_RE = re.compile(r"""^(?:"([^"]*)"|'([^']*)'|([A-Za-z0-9_.\-]+))\s*:\s*(.*)$""")


def _parse_key(content, lineno):
    m = _KEY_RE.match(content)
    if not m:
        raise YamlError(f"строка {lineno}: не 'ключ: значение': {content!r}")
    key = m.group(1) if m.group(1) is not None else (
        m.group(2) if m.group(2) is not None else m.group(3))
    return key, m.group(4)


def _logical_lines(text):
    """Текст -> [(lineno, indent, content)] без пустых и комментариев."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise YamlError(f"строка {n}: табуляция в отступе запрещена")
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        out.append((n, indent, stripped.strip()))
    return out


def _parse_block(lines, i, indent):
    """Парсит блок на уровне indent. Возвращает (значение, след. индекс)."""
    if i >= len(lines):
        return None, i
    n, ind, content = lines[i]
    if ind != indent:
        raise YamlError(f"строка {n}: ожидался отступ {indent}, got {ind}")
    if content == "-" or content.startswith("- "):
        return _parse_seq(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines, i, indent):
    result = {}
    while i < len(lines):
        n, ind, content = lines[i]
        if ind != indent:
            break
        if content == "-" or content.startswith("- "):
            break
        key, rest = _parse_key(content, n)
        if key in result:
            raise YamlError(f"строка {n}: дублирующийся ключ {key!r}")
        if rest:
            result[key] = _parse_scalar(rest, n)
            i += 1
        else:
            if i + 1 < len(lines) and lines[i + 1][1] > indent:
                value, i = _parse_block(lines, i + 1, lines[i + 1][1])
                result[key] = value
            else:
                result[key] = None
                i += 1
    return result, i


def _parse_seq(lines, i, indent):
    result = []
    while i < len(lines):
        n, ind, content = lines[i]
        if ind != indent or not (content == "-" or content.startswith("- ")):
            break
        rest = content[1:].lstrip()
        if not rest:
            if i + 1 < len(lines) and lines[i + 1][1] > indent:
                value, i = _parse_block(lines, i + 1, lines[i + 1][1])
                result.append(value)
            else:
                result.append(None)
                i += 1
            continue
        # "- key: value" — первый ключ отображения с виртуального отступа.
        if _KEY_RE.match(rest):
            pseudo = [(n, indent + 2, rest)] + lines[i + 1:]
            # Собираем только строки продолжения этого отображения.
            j = 1
            while j < len(pseudo) and pseudo[j][1] == indent + 2:
                j += 1
            mapping, _ = _parse_map(pseudo, 0, indent + 2)
            result.append(mapping)
            i += j
        else:
            result.append(_parse_scalar(rest, n))
            i += 1
    return result, i


def parse_yaml_subset(text):
    """Строгий парсер YAML-подмножества. Возвращает dict верхнего уровня."""
    lines = _logical_lines(text)
    if not lines:
        return {}
    value, i = _parse_block(lines, 0, lines[0][1])
    if i != len(lines):
        n = lines[i][0]
        raise YamlError(f"строка {n}: неожиданная строка после корня")
    if not isinstance(value, dict):
        raise YamlError("корень документа должен быть отображением")
    return value


# ---------------------------------------------------------------- YAML-дампер
def _needs_quotes(s):
    if s == "":
        return True
    if s != s.strip():
        return True
    if s in ("true", "True", "TRUE", "false", "False", "FALSE",
             "null", "Null", "NULL", "~"):
        return True
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return True
    if s[0] in "-?:,[]{}#&*!|>'\"%@`":
        return True
    if ": " in s or " #" in s or s.endswith(":"):
        return True
    return False


def _yaml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if _needs_quotes(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def dump_tasks_yaml(data):
    """Сериализует tasks-структуру в канонический вид парсера."""
    out = [f'version: "{data.get("version", SCHEMA_VERSION)}"']
    if data.get("updated") is not None:
        out.append(f"updated: {_yaml_scalar(data['updated'])}")
    out.append("tasks:")
    for t in data.get("tasks") or []:
        first = True
        for key in ("id", "title", "task", "workdir", "status", "kind",
                    "provider", "schedule", "due", "max_steps", "priority",
                    "created", "last_run", "last_result"):
            if key not in t or t[key] is None:
                continue
            val = _yaml_scalar(t[key])
            if first:
                out.append(f"  - {key}: {val}")
                first = False
            else:
                out.append(f"    {key}: {val}")
        if first:
            out.append("  - {}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- Валидация
def _parse_dt(value, field, errors, tid):
    if not isinstance(value, str):
        errors.append(f"{tid}: {field} — не строка: {value!r}")
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{tid}: {field} — не ISO-дата: {value!r}")
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SAMARA)
    return dt


def _valid_cron_field(field, lo, hi):
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            if not step.isdigit() or int(step) < 1:
                return False
            if base != "*" and not _valid_cron_field(base, lo, hi):
                return False
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                return False
            if not (lo <= int(a) <= hi and lo <= int(b) <= hi and int(a) <= int(b)):
                return False
            continue
        if not part.isdigit():
            return False
        if not (lo <= int(part) <= hi):
            return False
    return True


def validate_cron(expr):
    """Cron из 5 полей (мин час день месяц день-недели) или 'manual'."""
    if expr == "manual":
        return True
    if not isinstance(expr, str):
        return False
    parts = expr.split()
    if len(parts) != 5:
        return False
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    return all(_valid_cron_field(p, lo, hi) for p, (lo, hi) in zip(parts, ranges))


def _confined_workdir(workdir):
    """workdir обязан лежать внутри ~/workspace (realpath)."""
    base = os.path.realpath(os.path.expanduser("~/workspace"))
    p = os.path.realpath(os.path.expanduser(workdir))
    return p == base or p.startswith(base + os.sep)


def validate_tasks(data):
    """Строгая проверка схемы. Возвращает список ошибок (пусто = OK)."""
    errors = []
    if not isinstance(data, dict):
        return ["корень — не отображение"]
    unknown_top = set(data) - {"version", "updated", "tasks"}
    if unknown_top:
        errors.append(f"неизвестные ключи верхнего уровня: {sorted(unknown_top)}")
    if str(data.get("version")) != SCHEMA_VERSION:
        errors.append(f"version должен быть {SCHEMA_VERSION!r}, got {data.get('version')!r}")
    if "updated" in data and data["updated"] is not None:
        _parse_dt(data["updated"], "updated", errors, "файл")
    tasks = data.get("tasks")
    if tasks is None:
        tasks = []  # `tasks:` без элементов — пустой список, валидно
    if not isinstance(tasks, list):
        errors.append("tasks — не список")
        return errors
    seen = set()
    for idx, t in enumerate(tasks):
        tid = f"задача #{idx + 1}"
        if not isinstance(t, dict):
            errors.append(f"{tid}: не отображение")
            continue
        unknown = set(t) - TASK_FIELDS
        if unknown:
            errors.append(f"{tid}: неизвестные поля: {sorted(unknown)}")
        for f in TASK_REQUIRED:
            if f not in t or t[f] is None or (isinstance(t[f], str) and not t[f].strip()):
                errors.append(f"{tid}: нет обязательного поля {f!r}")
        tid_s = t.get("id")
        if isinstance(tid_s, str):
            tid = tid_s
            if not ID_RE.match(tid_s):
                errors.append(f"{tid}: id не [A-Za-z0-9][A-Za-z0-9_-]*: {tid_s!r}")
            if tid_s in seen:
                errors.append(f"{tid}: дублирующийся id")
            seen.add(tid_s)
        if "status" in t and t["status"] not in TASK_STATUSES:
            errors.append(f"{tid}: status не из {TASK_STATUSES}: {t['status']!r}")
        if "kind" in t and t["kind"] not in TASK_KINDS:
            errors.append(f"{tid}: kind не из {TASK_KINDS}: {t['kind']!r}")
        if "provider" in t and t["provider"] not in TASK_PROVIDERS:
            errors.append(f"{tid}: provider не из {TASK_PROVIDERS}: {t['provider']!r}")
        if "schedule" in t and not validate_cron(t["schedule"]):
            errors.append(f"{tid}: schedule не cron/manual: {t['schedule']!r}")
        if "due" in t and t["due"] is not None:
            _parse_dt(t["due"], "due", errors, tid)
        if "max_steps" in t and t["max_steps"] is not None:
            if (not isinstance(t["max_steps"], int)
                    or isinstance(t["max_steps"], bool)
                    or not 1 <= t["max_steps"] <= 100):
                errors.append(f"{tid}: max_steps — int 1..100: {t['max_steps']!r}")
        if "priority" in t and t["priority"] not in TASK_PRIORITIES:
            errors.append(f"{tid}: priority не из {TASK_PRIORITIES}: {t['priority']!r}")
        for f in ("created", "last_run"):
            if f in t and t[f] is not None:
                _parse_dt(t[f], f, errors, tid)
        if "last_result" in t and t["last_result"] is not None and not isinstance(t["last_result"], str):
            errors.append(f"{tid}: last_result — не строка")
        if isinstance(t.get("workdir"), str) and not _confined_workdir(t["workdir"]):
            errors.append(f"{tid}: workdir вне ~/workspace: {t['workdir']!r}")
    return errors


# ---------------------------------------------------------------- Загрузка/сохранение
def load_tasks(path=TASKS_PATH):
    """Читает и валидирует файл. Возвращает (data, errors)."""
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return None, [f"не читается {path}: {e}"]
    try:
        data = parse_yaml_subset(text)
    except YamlError as e:
        return None, [f"YAML: {e}"]
    errors = validate_tasks(data)
    return data, errors


def save_tasks(data, path=TASKS_PATH):
    """Атомарная запись (tmp + os.replace), updated — сейчас."""
    data = dict(data)
    data["updated"] = datetime.now(SAMARA).isoformat(timespec="seconds")
    path = os.path.expanduser(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(dump_tasks_yaml(data))
    os.replace(tmp, path)


def _now():
    return datetime.now(SAMARA)


# ---------------------------------------------------------------- Cron и due
def _cron_field_matches(field, value, lo, hi):
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            step = int(step)
            if base == "*":
                if (value - lo) % step == 0:
                    return True
            elif "-" in base:
                a, b = base.split("-", 1)
                if int(a) <= value <= int(b) and (value - int(a)) % step == 0:
                    return True
            elif base.isdigit() and int(base) == value:
                return True
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if int(a) <= value <= int(b):
                return True
            continue
        if part.isdigit() and int(part) == value:
            return True
    return False


def cron_matches(expr, dt):
    """Совпал ли cron с минутой dt (dt — aware)."""
    parts = expr.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = (
        dt.minute, dt.hour, dt.day, dt.month, dt.weekday())
    # cron: воскресенье = 0 и 7; weekday(): понедельник = 0.
    cron_dow = (dt.weekday() + 1) % 7
    return (
        _cron_field_matches(parts[0], minute, 0, 59)
        and _cron_field_matches(parts[1], hour, 0, 23)
        and _cron_field_matches(parts[2], dom, 1, 31)
        and _cron_field_matches(parts[3], month, 1, 12)
        and _cron_field_matches(parts[4], cron_dow, 0, 7)
    )


def is_due(task, now=None, window_min=DEFAULT_WINDOW_MIN):
    """Наступило ли время задачи (см. правила в шапке модуля)."""
    now = now or _now()
    if task.get("status") != "todo":
        return False
    due = task.get("due")
    if due is not None:
        dt = _parse_dt(due, "due", [], task.get("id", "?"))
        if dt is None:
            return False
        if dt > now:
            return False
        return True
    schedule = task.get("schedule", "manual")
    if schedule == "manual":
        return False
    last_run = task.get("last_run")
    if last_run is not None:
        lr = _parse_dt(last_run, "last_run", [], task.get("id", "?"))
        if lr is not None and lr > now - timedelta(minutes=window_min):
            return False
    probe = now
    for _ in range(window_min + 1):
        if cron_matches(schedule, probe):
            return True
        probe -= timedelta(minutes=1)
    return False


# ---------------------------------------------------------------- Runner
def _rotate_log(path, max_bytes=LOG_MAX_BYTES):
    try:
        if not os.path.isfile(path) or os.path.getsize(path) <= max_bytes:
            return
    except OSError:
        return
    old = path + ".1"
    if os.path.exists(old):
        try:
            if os.path.getmtime(old) < time.time() - 7 * 86400:
                os.remove(old)
            else:
                return
        except OSError:
            return
    try:
        os.replace(path, old)
    except OSError:
        pass


def _log_line(msg):
    os.makedirs(LOGS_DIR, exist_ok=True)
    _rotate_log(SCHEDULER_LOG)
    line = f"{_now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with open(SCHEDULER_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    print(msg)


def _acquire_lock():
    os.makedirs(LOGS_DIR, exist_ok=True)
    try:
        fd = os.open(SCHEDULER_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        except OSError:
            pass
        return fd
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(SCHEDULER_LOCK) > LOCK_STALE_S:
                os.remove(SCHEDULER_LOCK)
                return _acquire_lock()
        except OSError:
            pass
        return None
    except OSError:
        return None


def _release_lock(fd):
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(SCHEDULER_LOCK)
    except OSError:
        pass


def _run_one(task, log):
    """Один запуск задачи через fenrir_agent. Возвращает (ok, model_str)."""
    from fenrir_agent import build_chain, run_task
    kind = task.get("kind", "code")
    provider = task.get("provider", "klinch")
    chain = build_chain(kind, provider=provider)
    workdir = os.path.expanduser(task["workdir"])
    max_steps = task.get("max_steps") or DEFAULT_MAX_STEPS
    # У модели нет часов: подставляем текущее время Самары в текст
    # задачи, иначе даты в файлах она выдумывает (поймано живьём
    # 07.11.2026: STATUS.md получил «2025-01-18 14:30:00»).
    prompt = (f"Текущее время (Самара): "
              f"{_now().strftime('%Y-%m-%d %H:%M:%S %Z')}.\n"
              f"{task['task']}")
    ok, used = run_task(prompt, workdir, kind, chain, max_steps, log)
    model = f"{used[0]}/{used[1]}" if used else None
    return ok, model


def run_once(tasks_path=TASKS_PATH, dry_run=False, task_id=None,
             window_min=DEFAULT_WINDOW_MIN):
    """Один проход планировщика. Возвращает dict с итогом."""
    result = {"ran": 0, "ok": 0, "failed": 0, "skipped": 0, "errors": []}
    data, errors = load_tasks(tasks_path)
    if errors:
        for e in errors:
            _log_line(f"scheduler: ОШИБКА валидации {tasks_path}: {e}")
        result["errors"] = errors
        return result
    tasks = data.get("tasks") or []
    now = _now()
    if task_id:
        due = [t for t in tasks if t.get("id") == task_id]
        if not due:
            msg = f"задача {task_id} не найдена"
            _log_line(f"scheduler: {msg}")
            result["errors"].append(msg)
            return result
        if due[0].get("status") != "todo":
            msg = (f"задача {task_id} не todo "
                   f"(статус {due[0].get('status')}) — пропуск")
            _log_line(f"scheduler: {msg}")
            result["skipped"] = 1
            return result
    else:
        due = [t for t in tasks if is_due(t, now, window_min)]
    if dry_run:
        for t in due:
            _log_line(f"[DRY-RUN] выполнил бы {t['id']}: {t.get('title')}")
        result["ran"] = len(due)
        return result
    fd = _acquire_lock()
    if fd is None:
        msg = "уже запущен (lock) — пропуск"
        _log_line(f"scheduler: {msg}")
        result["errors"].append(msg)
        return result
    try:
        for t in due:
            tid = t["id"]
            _log_line(f"scheduler: старт {tid} "
                      f"({t.get('provider', 'klinch')}/{t.get('kind', 'code')}) "
                      f"workdir={t['workdir']}")
            try:
                ok, model = _run_one(t, lambda m: _log_line(f"  [{tid}] {m}"))
            except Exception as e:
                ok, model = False, None
                _log_line(f"scheduler: {tid} исключение: {type(e).__name__}: {e}")
            t["last_run"] = now.isoformat(timespec="seconds")
            t["last_result"] = (f"ok ({model})" if ok
                                else f"failed ({model or 'без модели'})")
            if t.get("schedule") not in (None, "manual"):
                t["status"] = "todo"  # рекуррентная: остаётся в очереди
            else:
                t["status"] = "done" if ok else "failed"
            result["ran"] += 1
            if ok:
                result["ok"] += 1
            else:
                result["failed"] += 1
            _log_line(f"scheduler: {tid} -> {t['status']} "
                      f"({t['last_result']})")
            save_tasks(data, tasks_path)
    finally:
        _release_lock(fd)
    return result


# ---------------------------------------------------------------- CLI: fen task
def _next_id(tasks):
    nums = []
    for t in tasks:
        m = re.fullmatch(r"FNR-(\d+)", t.get("id", ""))
        if m:
            nums.append(int(m.group(1)))
    return f"FNR-{max(nums, default=0) + 1:03d}"


def cmd_task_add(args):
    data, errors = load_tasks(args.tasks)
    if errors:
        print("ОШИБКА валидации tasks.yaml:")
        for e in errors:
            print(f"  - {e}")
        return 1
    tasks = data.get("tasks") or []
    data["tasks"] = tasks
    new = {
        "id": _next_id(tasks),
        "title": args.title,
        "task": args.task,
        "workdir": args.workdir,
        "status": "todo",
        "kind": args.kind,
        "provider": args.provider,
        "schedule": args.schedule,
        "created": _now().isoformat(timespec="seconds"),
    }
    if args.due:
        new["due"] = args.due
    if args.max_steps:
        new["max_steps"] = args.max_steps
    if args.priority:
        new["priority"] = args.priority
    tasks.append(new)
    errors = validate_tasks(data)
    if errors:
        print("ОШИБКА: новая задача не проходит валидацию:")
        for e in errors:
            print(f"  - {e}")
        return 1
    save_tasks(data, args.tasks)
    print(f"добавлено: {new['id']} — {new['title']} "
          f"(provider={new['provider']}, schedule={new['schedule']})")
    return 0


def cmd_task_done(args):
    data, errors = load_tasks(args.tasks)
    if errors:
        print("ОШИБКА валидации tasks.yaml:")
        for e in errors:
            print(f"  - {e}")
        return 1
    for t in data.get("tasks") or []:
        if t.get("id") == args.id:
            if t.get("status") == "done":
                print(f"{args.id} уже done")
                return 0
            t["status"] = "done"
            save_tasks(data, args.tasks)
            print(f"{args.id} -> done")
            return 0
    print(f"задача {args.id} не найдена")
    return 1


def cmd_task_list(args):
    data, errors = load_tasks(args.tasks)
    if errors:
        print("ОШИБКА валидации tasks.yaml:")
        for e in errors:
            print(f"  - {e}")
        return 1
    tasks = data.get("tasks") or []
    if not tasks:
        print("задач нет")
        return 0
    print(f"{'id':<9} {'статус':<7} {'провайдер':<8} {'расписание':<16} {'due':<25} заголовок")
    print("-" * 100)
    for t in tasks:
        print(f"{t.get('id', '?'):<9} {t.get('status', '?'):<7} "
              f"{t.get('provider', 'klinch'):<8} {t.get('schedule', 'manual'):<16} "
              f"{str(t.get('due') or '-'):<25} {t.get('title', '')}")
    return 0


def cmd_task_run(args):
    result = run_once(tasks_path=args.tasks, task_id=args.id)
    if result["errors"]:
        return 1
    return 0 if result["failed"] == 0 else 1


def cmd_task_validate(args):
    _data, errors = load_tasks(args.tasks)
    if errors:
        print("tasks.yaml НЕВАЛИДЕН:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"tasks.yaml валиден ({args.tasks})")
    return 0


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(prog="fenrir_scheduler")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_task = sub.add_parser("task", help="управление задачами")
    tsub = p_task.add_subparsers(dest="tcmd", required=True)
    p_add = tsub.add_parser("add", help="добавить задачу")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--task", required=True)
    p_add.add_argument("--workdir", required=True)
    p_add.add_argument("--schedule", default="manual")
    p_add.add_argument("--due", default=None)
    p_add.add_argument("--kind", default="code", choices=list(TASK_KINDS))
    p_add.add_argument("--provider", default="klinch",
                       choices=list(TASK_PROVIDERS))
    p_add.add_argument("--max-steps", type=int, default=None)
    p_add.add_argument("--priority", default=None, choices=list(TASK_PRIORITIES))
    p_add.set_defaults(fn=cmd_task_add)
    p_done = tsub.add_parser("done", help="пометить задачу done")
    p_done.add_argument("id")
    p_done.set_defaults(fn=cmd_task_done)
    p_list = tsub.add_parser("list", help="список задач")
    p_list.set_defaults(fn=cmd_task_list)
    p_run = tsub.add_parser("run", help="выполнить одну задачу сейчас")
    p_run.add_argument("id")
    p_run.set_defaults(fn=cmd_task_run)
    p_val = tsub.add_parser("validate", help="проверить tasks.yaml")
    p_val.set_defaults(fn=cmd_task_validate)
    for p in (p_add, p_done, p_list, p_run, p_val):
        p.add_argument("--tasks", default=TASKS_PATH)

    p_sched = sub.add_parser("run", help="проход планировщика")
    p_sched.add_argument("--once", action="store_true",
                         help="один проход (режим крона)")
    p_sched.add_argument("--dry-run", action="store_true")
    p_sched.add_argument("--tasks", default=TASKS_PATH)
    p_sched.add_argument("--task-id", default=None)
    p_sched.add_argument("--window-min", type=int, default=DEFAULT_WINDOW_MIN)

    a = ap.parse_args(argv)
    if a.cmd == "task":
        return a.fn(a)
    res = run_once(tasks_path=a.tasks, dry_run=a.dry_run,
                   task_id=a.task_id, window_min=a.window_min)
    print(f"scheduler: ran={res['ran']} ok={res['ok']} "
          f"failed={res['failed']} skipped={res['skipped']}")
    if res["errors"]:
        for e in res["errors"]:
            print(f"  ошибка: {e}")
        return 1
    return 0 if res["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
