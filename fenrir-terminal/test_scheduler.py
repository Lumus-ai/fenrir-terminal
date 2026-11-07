#!/usr/bin/env python3
"""Юнит-тесты ядра планировщика v1.4 (fenrir_scheduler.py).

Запуск: python3 test_scheduler.py
Сценарии: YAML-подмножество (парсер/дампер/round-trip), валидация
схемы, cron, due-логика, CLI add/done/list на временном файле.
Без сети и без моделей — только чистая логика.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fenrir_scheduler import (
    YamlError, cron_matches, dump_tasks_yaml, is_due, load_tasks,
    parse_yaml_subset, save_tasks, validate_cron, validate_tasks,
)

PASSED = 0


def check(name, got, want):
    global PASSED
    assert got == want, f"{name}: got {got!r}, want {want!r}"
    PASSED += 1
    print(f"ok: {name}")


def raises(name, fn):
    global PASSED
    try:
        fn()
    except (YamlError, ValueError):
        PASSED += 1
        print(f"ok: {name}")
        return
    raise AssertionError(f"{name}: ожидалась ошибка, но её нет")


SAMPLE = '''version: "1.4"
updated: 2026-11-07T15:00:00+04:00
tasks:
  - id: FNR-001
    title: "Проверка статуса"
    task: "Запиши в STATUS.md одну строку: время и OK."
    workdir: "~/workspace/fenrir-terminal/scheduler-tasks/status-check"
    status: todo
    kind: code
    provider: klinch
    schedule: manual
    max_steps: 15
    priority: P2
    created: 2026-11-07T15:00:00+04:00
  - id: FNR-002
    title: Вторая
    task: "Сделай файл report.txt"
    workdir: /home/hatch/workspace/x
    status: done
    schedule: "*/10 * * * *"
'''

# --- 1. Парсер ---
d = parse_yaml_subset(SAMPLE)
check("1. версия", d["version"], "1.4")
check("1b. задач", len(d["tasks"]), 2)
check("1c. id первой", d["tasks"][0]["id"], "FNR-001")
check("1d. max_steps int", d["tasks"][0]["max_steps"], 15)
check("1e. plain-строка с кавычками", d["tasks"][1]["title"], "Вторая")
check("1f. комментарий не мешает", d["tasks"][0]["schedule"], "manual")
check("1g. кириллица в кавычках", "Проверка статуса" in d["tasks"][0]["title"], True)

raises("1h. таб в отступе", lambda: parse_yaml_subset("a:\n\tb: 1\n"))
raises("1i. незакрытая кавычка",
       lambda: parse_yaml_subset('a: "не закрыта\n'))
raises("1j. дублирующийся ключ",
       lambda: parse_yaml_subset("a: 1\na: 2\n"))
raises("1k. блочный скаляр | не поддерживается",
       lambda: parse_yaml_subset("a: |\n  текст\n"))
raises("1l. flow-map не поддерживается",
       lambda: parse_yaml_subset("a: {x: 1}\n"))
raises("1m. якорь не поддерживается",
       lambda: parse_yaml_subset("a: &x 1\n"))

# --- 2. Дампер + round-trip ---
out = dump_tasks_yaml(d)
d2 = parse_yaml_subset(out)
check("2. round-trip версия", d2["version"], "1.4")
check("2b. round-trip задач", len(d2["tasks"]), 2)
check("2c. round-trip id", d2["tasks"][1]["id"], "FNR-002")
check("2d. round-trip max_steps", d2["tasks"][0]["max_steps"], 15)
check("2e. round-trip schedule", d2["tasks"][1]["schedule"], "*/10 * * * *")

# --- 3. Валидация ---
check("3. образец валиден", validate_tasks(d), [])
bad = parse_yaml_subset(SAMPLE)
bad["tasks"][0]["status"] = "waiting"
check("3b. плохой статус", len(validate_tasks(bad)), 1)
bad = parse_yaml_subset(SAMPLE)
bad["tasks"][0]["provider"] = "openai"
check("3c. плохой провайдер", len(validate_tasks(bad)), 1)
bad = parse_yaml_subset(SAMPLE)
bad["tasks"][1]["id"] = "FNR-001"
check("3d. дубль id", len(validate_tasks(bad)), 1)
bad = parse_yaml_subset(SAMPLE)
del bad["tasks"][0]["task"]
check("3e. нет обязательного поля", len(validate_tasks(bad)), 1)
bad = parse_yaml_subset(SAMPLE)
bad["tasks"][0]["workdir"] = "/etc/passwd"
check("3f. workdir вне ~/workspace", len(validate_tasks(bad)), 1)
bad = parse_yaml_subset(SAMPLE)
bad["tasks"][0]["schedule"] = "каждый день"
check("3g. плохой schedule", len(validate_tasks(bad)), 1)
bad = parse_yaml_subset(SAMPLE)
bad["tasks"][0]["due"] = "завтра"
check("3h. плохой due", len(validate_tasks(bad)), 1)
bad = parse_yaml_subset(SAMPLE)
bad["tasks"][0]["неизвестное"] = 1
check("3i. неизвестное поле", len(validate_tasks(bad)), 1)
bad = parse_yaml_subset(SAMPLE)
bad["version"] = "9.9"
check("3j. плохая версия", len(validate_tasks(bad)), 1)
bad = parse_yaml_subset(SAMPLE)
bad["tasks"][0]["max_steps"] = 500
check("3k. max_steps вне 1..100", len(validate_tasks(bad)), 1)

# --- 4. Cron ---
check("4. validate_cron */10", validate_cron("*/10 * * * *"), True)
check("4b. validate_cron manual", validate_cron("manual"), True)
check("4c. validate_cron мусор", validate_cron("каждый день"), False)
check("4d. validate_cron 4 поля", validate_cron("* * * *"), False)
check("4e. validate_cron 61 минута", validate_cron("61 * * * *"), False)
from zoneinfo import ZoneInfo
SAM = ZoneInfo("Europe/Samara")
dt = datetime(2026, 11, 7, 15, 30, tzinfo=SAM)
check("4f. cron */10 в 15:30", cron_matches("*/10 * * * *", dt), True)
check("4g. cron */10 в 15:31", cron_matches("*/10 * * * *",
      datetime(2026, 11, 7, 15, 31, tzinfo=SAM)), False)
check("4h. cron ежедневно 8:00",
      cron_matches("0 8 * * *", datetime(2026, 11, 7, 8, 0, tzinfo=SAM)), True)
check("4i. cron будни (сб — нет)",
      cron_matches("0 8 * * 1-5", datetime(2026, 11, 7, 8, 0, tzinfo=SAM)), False)

# --- 5. Due-логика ---
now = datetime(2026, 11, 7, 15, 30, tzinfo=SAM)
t = {"id": "x", "status": "todo", "schedule": "manual"}
check("5. manual без due — не due", is_due(t, now), False)
t = {"id": "x", "status": "todo", "schedule": "manual",
     "due": "2026-11-07T15:00:00+04:00"}
check("5b. due в прошлом — due", is_due(t, now), True)
t["due"] = "2026-11-07T16:00:00+04:00"
check("5c. due в будущем — не due", is_due(t, now), False)
t = {"id": "x", "status": "done", "schedule": "manual",
     "due": "2026-11-07T15:00:00+04:00"}
check("5d. статус done — не due", is_due(t, now), False)
t = {"id": "x", "status": "todo", "schedule": "*/10 * * * *"}
check("5e. cron в окне — due", is_due(t, now), True)
t["last_run"] = "2026-11-07T15:25:00+04:00"
check("5f. cron, но last_run в окне — не due", is_due(t, now), False)
t["last_run"] = "2026-11-07T14:00:00+04:00"
check("5g. cron, last_run давно — due", is_due(t, now), True)
t = {"id": "x", "status": "todo", "schedule": "0 8 * * *"}
check("5h. cron не в окне — не due", is_due(t, now), False)

# --- 6. CLI add/done/list на временном файле ---
import subprocess
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "tasks.yaml")
with open(path, "w", encoding="utf-8") as f:
    f.write('version: "1.4"\ntasks:\n')
r = subprocess.run([sys.executable, "fenrir_scheduler.py", "task", "add",
                    "--title", "Тест", "--task", "Сделай файл a.txt",
                    "--workdir", "~/workspace/fenrir-terminal/scheduler-test",
                    "--tasks", path],
                   capture_output=True, text=True,
                   cwd=os.path.dirname(os.path.abspath(__file__)))
check("6. add exit 0", r.returncode, 0)
check("6b. add выводит FNR-001", "FNR-001" in r.stdout, True)
data, errs = load_tasks(path)
check("6c. после add файл валиден", errs, [])
check("6d. одна задача", len(data["tasks"]), 1)
check("6e. provider по умолчанию klinch",
      data["tasks"][0]["provider"], "klinch")
r = subprocess.run([sys.executable, "fenrir_scheduler.py", "task", "list",
                    "--tasks", path], capture_output=True, text=True,
                   cwd=os.path.dirname(os.path.abspath(__file__)))
check("6f. list exit 0", r.returncode, 0)
check("6g. list показывает задачу", "FNR-001" in r.stdout and "Тест" in r.stdout, True)
r = subprocess.run([sys.executable, "fenrir_scheduler.py", "task", "done",
                    "FNR-001", "--tasks", path],
                   capture_output=True, text=True,
                   cwd=os.path.dirname(os.path.abspath(__file__)))
check("6h. done exit 0", r.returncode, 0)
data, _ = load_tasks(path)
check("6i. статус стал done", data["tasks"][0]["status"], "done")
r = subprocess.run([sys.executable, "fenrir_scheduler.py", "task", "validate",
                    "--tasks", path], capture_output=True, text=True,
                   cwd=os.path.dirname(os.path.abspath(__file__)))
check("6j. validate exit 0", r.returncode, 0)
# Битый файл — validate должен упасть.
with open(path, "w", encoding="utf-8") as f:
    f.write('version: "1.4"\ntasks:\n  - id: FNR-001\n    status: waiting\n')
r = subprocess.run([sys.executable, "fenrir_scheduler.py", "task", "validate",
                    "--tasks", path], capture_output=True, text=True,
                   cwd=os.path.dirname(os.path.abspath(__file__)))
check("6k. validate битого файла exit 1", r.returncode, 1)

print(f"\nВсе тесты планировщика прошли: {PASSED} проверок OK.")
