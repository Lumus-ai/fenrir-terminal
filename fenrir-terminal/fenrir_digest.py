#!/usr/bin/env python3
"""Fenrir Terminal v1.4 — утренний дайджест (stdlib only).

Что это: сводка на утро по tasks.yaml и логам планировщика.
Триггер — cron 07:30 + при первом открытии терминала
(`fen morning`). Крон НЕ создаётся здесь (его ставит человек):
    30 7 * * * /home/hatch/workspace/fenrir-terminal/bin/fenrir morning

Вывод — stdout + файл digests/YYYY-MM-DD.md (перезапись за тот же
день идемпотентна: один файл на день, повторов нет).

Формат — по спеке v1.4 (PLAN-EXTENDED.md, раздел «2. Утренний
дайджест»); спека обрывается на середине, поэтому формат доработан
по смыслу: просроченные / на сегодня / P0 / выполнено вчера /
последняя активность / система / недавно вшито.

Источники данных (только чтение, задачи не меняются):
- tasks.yaml — парсер и валидация переиспользуются из
  fenrir_scheduler.py (load_tasks), свой парсер НЕ пишется;
- logs/scheduler.log — что выполнялось (вчерашние done и окно
  активности берутся по датам строк лога);
- CHANGELOG.md — заголовок последней секции («недавно вшито»);
- serve /health, RAM и отсчёт Lix — живые, через fenrir_term.py
  (ram_guard, lix_countdown) и urllib.

Честные ограничения (не выдумываем данных):
- «Активность» — это окно первой/последней строки scheduler.log
  за вчера + число запусков, а НЕ часы работы: часов активности
  в логах нет, поэтому «6ч 20м» из спеки НЕ считается и НЕ пишется.
- Вчера выполнено считается по строкам лога
  «scheduler: <id> -> done» за вчера; задачи done без last_run
  (например, помеченные вручную через `fen task done`) в счёт
  вчера не попадают — у них нет даты выполнения.
- Если tasks.yaml битый или отсутствует — дайджест не падает:
  раздел задач показывает ошибку валидации, остальные разделы
  считаются как обычно.
"""
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

from fenrir_worklog import worklog

try:
    from zoneinfo import ZoneInfo
    SAMARA = ZoneInfo("Europe/Samara")
except Exception:
    SAMARA = timezone(timedelta(hours=4))

TERM_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_PATH = os.path.join(TERM_DIR, "tasks.yaml")
SCHEDULER_LOG = os.path.join(TERM_DIR, "logs", "scheduler.log")
CHANGELOG_PATH = os.path.join(TERM_DIR, "CHANGELOG.md")
DIGESTS_DIR = os.path.join(TERM_DIR, "digests")

WEEKDAYS_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\s+(.*)$")
LOG_DONE_RE = re.compile(r"scheduler: (\S+) -> done\b")
LOG_FAILED_RE = re.compile(r"scheduler: (\S+) -> failed\b")


def _parse_dt(value):
    """ISO-строка -> datetime (aware, Самара) или None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SAMARA)
    return dt.astimezone(SAMARA)


def _read_log_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _system_status():
    """Живой статус: serve, RAM, дедлайн Lix. Только чтение."""
    serve = "не отвечает"
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:4097/health", timeout=3) as resp:
            import json
            data = json.load(resp)
            serve = "OK" if data.get("ok") else "отвечает, ok=false"
    except Exception:
        serve = "не отвечает"
    try:
        from fenrir_term import lix_countdown, ram_guard
        mb, ram_status = ram_guard()
        ram = f"{mb} МБ [{ram_status}]"
        lix = lix_countdown()
    except Exception:
        ram, lix = "нет данных", "нет данных"
    return {"serve": serve, "ram": ram, "lix": lix}


def _latest_changelog_section(path=CHANGELOG_PATH):
    """Заголовок первой секции CHANGELOG.md (последнее вшитое)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("## "):
                    return line[3:].strip()
    except OSError:
        return None
    return None


def collect_stats(tasks_path=TASKS_PATH, log_path=SCHEDULER_LOG, day=None):
    """Считает всё для дайджеста на дату day (по умолчанию сегодня)."""
    from fenrir_scheduler import load_tasks

    day = day or datetime.now(SAMARA).date()
    yesterday = day - timedelta(days=1)
    data, errors = load_tasks(tasks_path)
    tasks = []
    if data is not None:
        # Битый файл: load_tasks возвращает data + errors; tasks может
        # быть не списком (например, строка) — тогда считаем задач нет,
        # а ошибка показывается в дайджесте отдельно.
        raw = data.get("tasks")
        if isinstance(raw, list):
            tasks = [t for t in raw if isinstance(t, dict)]

    def due_date(t):
        dt = _parse_dt(t.get("due"))
        return dt.date() if dt else None

    def last_run_date(t):
        dt = _parse_dt(t.get("last_run"))
        return dt.date() if dt else None

    overdue, due_today, p0 = [], [], []
    for t in tasks:
        if t.get("status") != "todo":
            continue
        if t.get("priority") == "P0":
            p0.append(t)
        d = due_date(t)
        if d is not None and d < day:
            overdue.append(t)
        elif d == day:
            due_today.append(t)

    # Вчера по логу планировщика: done/failed и окно активности.
    log_lines = _read_log_lines(log_path)
    done_yday, failed_yday = [], []
    yday_times = []
    last_line = None
    for line in log_lines:
        m = LOG_LINE_RE.match(line)
        if not m:
            continue
        line_date = date.fromisoformat(m.group(1))
        line_time = m.group(2)
        body = m.group(3)
        last_line = (line_date, line_time, body)
        if line_date != yesterday:
            continue
        yday_times.append(line_time)
        dm = LOG_DONE_RE.search(body)
        if dm:
            done_yday.append(dm.group(1))
        fm = LOG_FAILED_RE.search(body)
        if fm:
            failed_yday.append(fm.group(1))

    # Выполнено вчера по tasks.yaml (last_run вчера) — как доп. срез:
    # берём только те, что уже есть в done_yday, чтобы не дублировать.
    done_titles = {}
    for t in tasks:
        if t.get("status") == "done" and last_run_date(t) == yesterday:
            done_titles[t.get("id")] = t.get("title", "")

    return {
        "day": day,
        "yesterday": yesterday,
        "tasks_error": errors or None,
        "overdue": overdue,
        "due_today": due_today,
        "p0": p0,
        "done_yday": done_yday,
        "failed_yday": failed_yday,
        "done_titles": done_titles,
        "yday_first": yday_times[0] if yday_times else None,
        "yday_last": yday_times[-1] if yday_times else None,
        "yday_lines": len(yday_times),
        "last_activity": last_line,
        "total_todo": sum(1 for t in tasks if t.get("status") == "todo"),
    }


def render_digest(stats):
    """Текст дайджеста по собранной статистике."""
    day = stats["day"]
    wd = WEEKDAYS_RU[day.weekday()]
    lines = []
    lines.append(f"🐺 ФЕНРИР — {day.strftime('%d.%m')} ({wd})")
    lines.append("─" * 30)
    lines.append(
        f"⚠ Просрочено: {len(stats['overdue'])} | "
        f"Сегодня: {len(stats['due_today'])} | "
        f"P0: {len(stats['p0'])}")
    n_done = len(stats["done_yday"])
    act = ""
    if stats["yday_first"] and stats["yday_last"]:
        act = (f", активность {stats['yday_first'][:5]}–"
               f"{stats['yday_last'][:5]}")
    lines.append(f"✔ Вчера: {n_done} задач выполнено{act}")
    if stats["failed_yday"]:
        lines.append(f"✘ Вчера упало: {len(stats['failed_yday'])} "
                     f"({', '.join(stats['failed_yday'])})")
    lines.append("")

    if stats["tasks_error"]:
        lines.append("⚠ tasks.yaml — ошибка валидации:")
        for e in stats["tasks_error"]:
            lines.append(f"  - {e}")
        lines.append("")

    def section(title, items, empty="нет"):
        lines.append(f"{title}:")
        if not items:
            lines.append(f"  {empty}")
            return
        for t in items:
            if isinstance(t, dict):
                extra = ""
                if t.get("due"):
                    extra = f" (due {str(t['due'])[:10]})"
                lines.append(f"  - {t.get('id')}: {t.get('title')}{extra}")
            else:
                title_text = stats["done_titles"].get(t, "")
                suffix = f" — {title_text}" if title_text else ""
                lines.append(f"  - {t}{suffix}")

    section("📋 Просрочено", stats["overdue"])
    section("📅 Сегодня", stats["due_today"])
    section("🔴 P0", stats["p0"])
    section("✔ Вчера выполнено", stats["done_yday"])
    lines.append("")
    last = stats["last_activity"]
    if last:
        lines.append(f"🕐 Последняя активность: {last[0]} {last[1][:5]} — "
                     f"{last[2][:120]}")
    else:
        lines.append("🕐 Последняя активность: нет записей в scheduler.log")
    sys_status = _system_status()
    lines.append(f"🟢 Система: serve {sys_status['serve']}, "
                 f"RAM {sys_status['ram']}, {sys_status['lix']}")
    latest = _latest_changelog_section()
    if latest:
        lines.append(f"📰 Недавно вшито: {latest}")
    lines.append("")
    lines.append(f"Всего задач todo: {stats['total_todo']}")
    return "\n".join(lines) + "\n"


def generate(tasks_path=TASKS_PATH, log_path=SCHEDULER_LOG,
            day=None, out_dir=DIGESTS_DIR):
    """Собирает дайджест, пишет файл digests/YYYY-MM-DD.md, возвращает текст."""
    day = day or datetime.now(SAMARA).date()
    stats = collect_stats(tasks_path, log_path, day)
    text = render_digest(stats)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{day.isoformat()}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return text, out_path


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="fenrir_digest")
    ap.add_argument("--tasks", default=TASKS_PATH)
    ap.add_argument("--log", default=SCHEDULER_LOG)
    ap.add_argument("--date", default=None,
                    help="дата дайджеста YYYY-MM-DD (по умолчанию сегодня)")
    ap.add_argument("--out-dir", default=DIGESTS_DIR)
    ap.add_argument("--no-write", action="store_true",
                    help="только stdout, без записи файла")
    a = ap.parse_args(argv)
    day = None
    if a.date:
        try:
            day = date.fromisoformat(a.date)
        except ValueError:
            print(f"digest: неверная дата {a.date!r}, нужен YYYY-MM-DD",
                  file=sys.stderr)
            return 1
    if a.no_write:
        stats = collect_stats(a.tasks, a.log, day)
        print(render_digest(stats), end="")
        return 0
    text, out_path = generate(a.tasks, a.log, day, a.out_dir)
    print(text, end="")
    print(f"digest: записано в {out_path}")
    # v1.4 авто-журлог: дайджест собран и записан (--no-write не
    # логируется: файл не создан, работа не выполнена).
    worklog("digest", "утренний дайджест", "ok",
            f"записано в {os.path.basename(out_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
