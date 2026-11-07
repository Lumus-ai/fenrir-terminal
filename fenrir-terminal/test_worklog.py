#!/usr/bin/env python3
"""Тесты авто-журлога v1.4 (fenrir_worklog.py + точки встройки).

Запуск: python3 test_worklog.py
Все тесты работают только с временными файлами — живой WORK_LOG.md
не трогается (путь журнала переопределяется аргументом path).
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fenrir_worklog import format_entry, worklog  # noqa: E402
import fenrir_agent  # noqa: E402
import fenrir_scheduler  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    SAMARA = ZoneInfo("Europe/Samara")
except Exception:
    SAMARA = timezone(timedelta(hours=4))

PASSED = 0
FAILED = 0


def check(name, cond, extra=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok: {name}")
    else:
        FAILED += 1
        print(f"  FAIL: {name} {extra}")


def tmp_path():
    fd, path = tempfile.mkstemp(prefix="worklog-test-", suffix=".md")
    os.close(fd)
    os.remove(path)  # worklog должен сам создать файл с заголовком
    return path


WHEN = datetime(2026, 11, 7, 15, 52, tzinfo=SAMARA)
WHEN2 = datetime(2026, 11, 7, 15, 53, tzinfo=SAMARA)


def test_format():
    print("формат записи (v1.4):")
    line = format_entry("scheduler", "TST-002 Проверка статуса",
                        "ok", "ok (custom.klinch/gpt-6-luna)", WHEN)
    check("дата и время (Самара)",
          line.startswith("2026-11-07 | 15:52 | "))
    check("7 полей через |", len(line.split(" | ")) == 7,
          f"полей: {len(line.split(' | '))}")
    check("исполнитель", " | scheduler | " in line)
    check("статус ok", " | ok | " in line)
    check("пометка [авто]", line.endswith(" | [авто]"))
    check("одна строка", "\n" not in line)


def test_clean():
    print("очистка полей:")
    line = format_entry("a|b", "задача\nс переводом | и палкой", "ok",
                        "прим\nечание", WHEN)
    check("нет переводов строк", "\n" not in line)
    check("палки заменены", line.count(" | ") == 6)
    long_note = "x" * 500
    line2 = format_entry("a", "t", "ok", long_note, WHEN)
    check("примечание обрезано до 200",
          len(line2.split(" | ")[5]) <= 200)


def test_create_and_dedup():
    print("создание файла и дедуп:")
    p = tmp_path()
    try:
        ok = worklog("scheduler", "TST-001 Тест", "ok", "ok (m)", WHEN, p)
        check("первая запись пишется", ok is True)
        check("файл создан с заголовком",
              open(p, encoding="utf-8").read().startswith("# WORK_LOG"))
        check("строка в файле",
              "2026-11-07 | 15:52 | scheduler | TST-001 Тест | ok |" in
              open(p, encoding="utf-8").read())
        ok2 = worklog("scheduler", "TST-001 Тест", "ok", "ok (m)", WHEN, p)
        check("повтор той же строки — дубль, не пишется", ok2 is False)
        lines = [l for l in open(p, encoding="utf-8") if "TST-001" in l]
        check("в файле ровно одна строка TST-001", len(lines) == 1,
              f"строк: {len(lines)}")
        ok3 = worklog("scheduler", "TST-001 Тест", "ok", "ok (m)", WHEN2, p)
        check("та же задача в другую минуту — новая строка", ok3 is True)
        ok4 = worklog("backup", "fenrir backup", "failed", "секреты", WHEN, p)
        check("другой исполнитель — пишется", ok4 is True)
    finally:
        if os.path.exists(p):
            os.remove(p)


def test_no_raise_on_bad_path():
    print("устойчивость к ошибкам:")
    bad = "/nonexistent-dir-xyz/WORK_LOG.md"
    try:
        ok = worklog("agent", "t", "ok", "n", WHEN, bad)
        check("битый путь не роняет вызов, возвращает False", ok is False)
    except Exception as e:  # noqa: BLE001
        check("битый путь не роняет вызов", False, str(e))


def test_agent_title():
    print("заголовок задачи агента:")
    check("префикс времени отбрасывается",
          fenrir_agent._agent_title(
              "Текущее время (Самара): 2026-11-07 15:52:00 +04.\n"
              "Проверь STATUS.md") == "Проверь STATUS.md")
    check("обычный текст — первая строка",
          fenrir_agent._agent_title("Сделай сайт\nвторой строкой") ==
          "Сделай сайт")
    check("пустая задача", fenrir_agent._agent_title("") == "(без названия)")


def test_agent_wrapper_logs():
    print("агент: обёртка run_task пишет строку в журлог:")
    with tempfile.TemporaryDirectory() as d:
        wl = os.path.join(d, "WORK_LOG.md")
        orig_run = fenrir_agent._run_task
        orig_env = os.environ.get("FENRIR_WORKLOG")
        os.environ["FENRIR_WORKLOG"] = wl
        try:
            fenrir_agent._run_task = lambda *a, **k: (
                True, ("custom.klinch", "gpt-6-luna"))
            ok, used = fenrir_agent.run_task(
                "Текущее время (Самара): 2026-11-07 15:52:00 +04.\nСделай X",
                "/tmp", "code", [], 1, lambda s: None)
            check("run_task вернул ok", ok is True)
            content = open(wl, encoding="utf-8").read()
            lines = [l for l in content.splitlines() if " | agent | " in l]
            check("одна строка agent", len(lines) == 1,
                  f"строк: {len(lines)}")
            check("заголовок задачи без префикса времени",
                  "Сделай X" in lines[0] if lines else False)
            check("модель в примечании",
                  "custom.klinch/gpt-6-luna" in lines[0] if lines else False)
            # log_worklog=False — планировщик логирует сам, дублей нет.
            fenrir_agent.run_task("Сделай Y", "/tmp", "code", [], 1,
                                  lambda s: None, log_worklog=False)
            content2 = open(wl, encoding="utf-8").read()
            check("log_worklog=False — строк не добавилось",
                  content2 == content)
        finally:
            fenrir_agent._run_task = orig_run
            if orig_env is None:
                os.environ.pop("FENRIR_WORKLOG", None)
            else:
                os.environ["FENRIR_WORKLOG"] = orig_env


def test_scheduler_writes_once():
    print("планировщик: одна строка на выполненную задачу:")
    with tempfile.TemporaryDirectory() as d:
        tasks_path = os.path.join(d, "tasks.yaml")
        wl_path = os.path.join(d, "WORK_LOG.md")
        with open(tasks_path, "w", encoding="utf-8") as f:
            f.write('version: "1.4"\nupdated: 2026-11-07T15:40:00+04:00\n'
                    "tasks:\n"
                    "  - id: TST-900\n"
                    '    title: "Тестовая задача"\n'
                    '    task: "ничего"\n'
                    '    workdir: "~/workspace/fenrir-terminal/scheduler-test/work"\n'
                    "    status: todo\n"
                    "    kind: code\n"
                    "    provider: klinch\n"
                    "    schedule: manual\n"
                    "    max_steps: 1\n")
        # Подменяем _run_one: без вызова моделей.
        orig = fenrir_scheduler._run_one
        calls = []

        def fake_run_one(task, log):
            calls.append(task["id"])
            return True, "custom.klinch/gpt-6-luna"

        fenrir_scheduler._run_one = fake_run_one
        try:
            res = fenrir_scheduler.run_once(
                tasks_path=tasks_path, task_id="TST-900",
                worklog_path=wl_path)
        finally:
            fenrir_scheduler._run_one = orig
        check("run_once выполнил задачу", res["ran"] == 1 and res["ok"] == 1,
              str(res))
        content = open(wl_path, encoding="utf-8").read()
        lines = [l for l in content.splitlines() if "TST-900" in l]
        check("в журлоге ровно одна строка TST-900", len(lines) == 1,
              f"строк: {len(lines)}")
        check("формат v1.4 (7 полей)",
              len(lines[0].split(" | ")) == 7 if lines else False)
        check("статус ok в строке", " | ok | " in lines[0] if lines else False)
        # Повторный прогон: задача уже done — пропуск, новых строк нет.
        res2 = fenrir_scheduler.run_once(
            tasks_path=tasks_path, task_id="TST-900", worklog_path=wl_path)
        check("повторный прогон — пропуск (не todo)",
              res2["skipped"] == 1, str(res2))
        content2 = open(wl_path, encoding="utf-8").read()
        check("после пропуска строк не добавилось",
              content2 == content)


def test_dry_run_writes_nothing():
    print("dry-run планировщика ничего не пишет:")
    with tempfile.TemporaryDirectory() as d:
        tasks_path = os.path.join(d, "tasks.yaml")
        wl_path = os.path.join(d, "WORK_LOG.md")
        with open(tasks_path, "w", encoding="utf-8") as f:
            f.write('version: "1.4"\nupdated: 2026-11-07T15:40:00+04:00\n'
                    "tasks:\n"
                    "  - id: TST-901\n"
                    '    title: "Тестовая задача"\n'
                    '    task: "ничего"\n'
                    '    workdir: "~/workspace/fenrir-terminal/scheduler-test/work"\n'
                    "    status: todo\n"
                    "    kind: code\n"
                    "    provider: klinch\n"
                    "    schedule: manual\n"
                    "    max_steps: 1\n")
        res = fenrir_scheduler.run_once(
            tasks_path=tasks_path, task_id="TST-901", dry_run=True,
            worklog_path=wl_path)
        check("dry-run нашёл задачу", res["ran"] == 1, str(res))
        check("журлог не создан при dry-run", not os.path.exists(wl_path))


def main():
    test_format()
    test_clean()
    test_create_and_dedup()
    test_no_raise_on_bad_path()
    test_agent_title()
    test_agent_wrapper_logs()
    test_scheduler_writes_once()
    test_dry_run_writes_nothing()
    print(f"\nИТОГО: {PASSED} пройдено, {FAILED} провалено")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
