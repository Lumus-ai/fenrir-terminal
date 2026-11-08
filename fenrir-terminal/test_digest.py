#!/usr/bin/env python3
"""Тесты утреннего дайджеста v1.4 (stdlib only, без модели).

Проверяется на синтетических tasks.yaml / scheduler.log во временной
папке: счётчики просрочено/сегодня/P0/вчера, окно активности,
идемпотентность файла за день, битый tasks.yaml, пустые данные.
"""
import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fenrir_digest as dg

TASKS_YAML = """version: "1.4"
updated: 2026-11-07T10:00:00+04:00
tasks:
  - id: WRK-OVER1
    title: "Просроченная раз"
    task: "Сделай раз."
    workdir: "~/workspace/fenrir-terminal/scheduler-tasks/t1"
    status: todo
    kind: code
    provider: klinch
    schedule: manual
    due: 2026-11-05T10:00:00+04:00
    priority: P1
    created: 2026-11-05T09:00:00+04:00
  - id: WRK-OVER2
    title: "Просроченная два"
    task: "Сделай два."
    workdir: "~/workspace/fenrir-terminal/scheduler-tasks/t2"
    status: todo
    kind: code
    provider: klinch
    schedule: manual
    due: 2026-11-06T10:00:00+04:00
    priority: P2
    created: 2026-11-06T09:00:00+04:00
  - id: WRK-TODAY
    title: "На сегодня"
    task: "Сделай сегодня."
    workdir: "~/workspace/fenrir-terminal/scheduler-tasks/t3"
    status: todo
    kind: code
    provider: klinch
    schedule: manual
    due: 2026-11-07T18:00:00+04:00
    priority: P2
    created: 2026-11-07T09:00:00+04:00
  - id: WRK-P0
    title: "Критичная"
    task: "Сделай срочно."
    workdir: "~/workspace/fenrir-terminal/scheduler-tasks/t4"
    status: todo
    kind: code
    provider: klinch
    schedule: manual
    priority: P0
    created: 2026-11-07T09:00:00+04:00
  - id: WRK-DONEY
    title: "Выполненная вчера"
    task: "Была выполнена."
    workdir: "~/workspace/fenrir-terminal/scheduler-tasks/t5"
    status: done
    kind: code
    provider: klinch
    schedule: manual
    priority: P2
    created: 2026-11-06T09:00:00+04:00
    last_run: 2026-11-06T12:30:00+04:00
    last_result: "ok"
"""

LOG_TEXT = """2026-11-06 09:15:00   scheduler: WRK-DONEY -> done (ok (custom.klinch/gpt-6-luna))
2026-11-06 12:30:00   scheduler: WRK-OTHER -> failed (error)
2026-11-06 13:00:00   scheduler: TST-GHOST -> done (ok)
2026-11-06 13:05:00   scheduler: задача TST-900 не todo (статус done) — пропуск
2026-11-07 08:00:00   scheduler: WRK-TODAY -> done (ok)
"""

DAY = date(2026, 11, 7)


def _write(tmp, name, text):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def run():
    passed = failed = 0

    def check(name, cond, extra=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  OK   {name}")
        else:
            failed += 1
            print(f"  FAIL {name} {extra}")

    with tempfile.TemporaryDirectory() as tmp:
        tasks = _write(tmp, "tasks.yaml", TASKS_YAML)
        log = _write(tmp, "scheduler.log", LOG_TEXT)
        out_dir = os.path.join(tmp, "digests")

        st = dg.collect_stats(tasks, log, DAY)
        check("просрочено = 2", len(st["overdue"]) == 2,
              f"got {len(st['overdue'])}")
        check("сегодня = 1", len(st["due_today"]) == 1)
        check("P0 = 1", len(st["p0"]) == 1)
        check("вчера done по логу = 1 (WRK-DONEY)",
              st["done_yday"] == ["WRK-DONEY"], f"got {st['done_yday']}")
        check("вчера failed по логу = 1 (WRK-OTHER)",
              st["failed_yday"] == ["WRK-OTHER"])
        check("тестовые TST-* в done вчера не попадают",
              "TST-GHOST" not in st["done_yday"]
              and "TST-900" not in st["done_yday"],
              f"got {st['done_yday']}")
        check("тестовые TST-* не сдвигают окно активности",
              st["yday_first"] == "09:15:00" and st["yday_last"] == "12:30:00",
              f"got {st['yday_first']}–{st['yday_last']}")
        check("окно активности вчера 09:15–12:30",
              st["yday_first"] == "09:15:00" and st["yday_last"] == "12:30:00",
              f"got {st['yday_first']}–{st['yday_last']}")
        check("todo всего = 4", st["total_todo"] == 4)
        check("нет ошибки валидации", st["tasks_error"] is None)
        check("заголовок done вчера по tasks.yaml",
              st["done_titles"].get("WRK-DONEY") == "Выполненная вчера")

        text = dg.render_digest(st)
        check("заголовок даты 07.11 (сб)",
              "🐺 ФЕНРИР — 07.11 (сб)" in text)
        check("строка счётчиков",
              "⚠ Просрочено: 2 | Сегодня: 1 | P0: 1" in text, text[:200])
        check("строка вчера с окном активности",
              "✔ Вчера: 1 задач выполнено, активность 09:15–12:30" in text)
        check("нет выдуманных часов активности",
              "6ч" not in text and "активность 6" not in text)
        check("раздел просроченных содержит оба id",
              "WRK-OVER1" in text and "WRK-OVER2" in text)
        check("раздел P0 содержит WRK-P0", "WRK-P0" in text)
        check("в тексте дайджеста нет тестовых TST-*",
              "TST-GHOST" not in text and "TST-900" not in text)

        # Генерация файла + идемпотентность.
        text1, path1 = dg.generate(tasks, log, DAY, out_dir)
        check("файл создан digests/2026-11-07.md",
              path1 == os.path.join(out_dir, "2026-11-07.md")
              and os.path.exists(path1))
        with open(path1, encoding="utf-8") as f:
            content1 = f.read()
        text2, path2 = dg.generate(tasks, log, DAY, out_dir)
        with open(path2, encoding="utf-8") as f:
            content2 = f.read()
        # Содержимое может отличаться только живой строкой системы
        # (RAM меняется между прогонами) — сравниваем без неё.
        def _stable(c):
            return "\n".join(l for l in c.splitlines()
                             if not l.startswith("🟢 Система:"))
        check("повторная генерация — тот же файл, содержимое стабильно",
              path1 == path2 and _stable(content1) == _stable(content2))
        check("в файле нет дублей заголовка",
              content1.count("🐺 ФЕНРИР") == 1)

        # Пустые данные.
        empty_tasks = _write(tmp, "empty.yaml",
                             'version: "1.4"\nupdated: 2026-11-07T10:00:00+04:00\ntasks:\n')
        empty_log = _write(tmp, "empty.log", "")
        st2 = dg.collect_stats(empty_tasks, empty_log, DAY)
        check("пусто: все счётчики 0",
              len(st2["overdue"]) == 0 and len(st2["due_today"]) == 0
              and len(st2["p0"]) == 0 and len(st2["done_yday"]) == 0)
        t2 = dg.render_digest(st2)
        check("пусто: разделы пишут «нет»",
              t2.count("  нет") >= 3)

        # Битый tasks.yaml — дайджест не падает, показывает ошибку.
        bad = _write(tmp, "bad.yaml", "version: \"9.9\"\ntasks: not-a-list\n")
        st3 = dg.collect_stats(bad, empty_log, DAY)
        check("битый tasks.yaml: есть tasks_error",
              bool(st3["tasks_error"]))
        t3 = dg.render_digest(st3)
        check("битый tasks.yaml: ошибка показана в тексте",
              "ошибка валидации" in t3)

        # Отсутствующий лог — не падает.
        st4 = dg.collect_stats(tasks, os.path.join(tmp, "no-such.log"), DAY)
        check("нет лога: последняя активность — нет записей",
              st4["last_activity"] is None)

    print(f"\nИТОГО: {passed} пройдено, {failed} провалено")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
