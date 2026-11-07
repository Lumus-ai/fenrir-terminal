#!/usr/bin/env python3
"""Тесты консилиума v3.1 (fenrir_orchestra.py council31 + fenrir_council.py).

Живые вызовы моделей не делаются, кроме помеченных live-прогонов вне
этого файла. Запуск: python3 test_council.py
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fenrir_orchestra as orch

PASSED = []
FAILED = []


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(f"{'OK  ' if cond else 'FAIL'} {name} {extra if not cond else ''}")


FAKE_CFG = {
    "roles": {
        "code": ["lix/deepseek-v4.1-flash", "lix/kimi-k3",
                 "lix/qwen3.8-max", "lix/deepseek-v4-pro"],
        "review": ["lix/glm-5.3-prime", "lix/deepseek-v4-pro", "lix/kimi-k3"],
        "text": ["lix/kimi-k3", "lix/deepseek-v4.1-flash"],
    },
    "providers": {"lix": {"base_url": "https://lix.autos/v1",
                          "connector": "custom.lix",
                          "allowed_hosts": ["lix.autos"]}},
    "lix": {"deadline": "2026-11-09T01:21:00+04:00"},
}
DEAD_TRIO = {"claude-opus-5.5", "claude-sonnet-5.5", "gpt-6-astra"}

# --- 1. Таблица ролей v3.1 -------------------------------------------------
names = [r[0] for r in orch.COUNCIL_V31_ROLES]
check("roles: ровно 3 роли", len(orch.COUNCIL_V31_ROLES) == 3)
check("roles: кодер/ревьюер/тестер",
      names == ["coder", "reviewer", "tester"], str(names))
pinned = [r[2] for r in orch.COUNCIL_V31_ROLES]
check("roles: закреплённые модели различны", len(set(pinned)) == 3, str(pinned))
check("roles: все закреплённые — живые Lix (не мёртвая тройка)",
      all(p.split("/", 1)[1] not in DEAD_TRIO and p.startswith("lix/")
          for p in pinned), str(pinned))
check("roles: параллельный лимит ровно 3 (MAX_PARALLEL)",
      orch.COUNCIL_V31_PARALLEL == 3 == orch.MAX_PARALLEL)

# --- 2. Цепочка с закреплённой моделью ------------------------------------
_real_chain = orch.chain_for_role
_real_cooldown = orch.is_cooldown
_real_dead = orch._router_dead
orch._router_dead = lambda: DEAD_TRIO
orch.is_cooldown = lambda k: False
ch = orch.chain_for_role_pinned(FAKE_CFG, "code", "lix/deepseek-v4-pro")
check("pinned: закреплённая модель первая",
      ch and ch[0] == ("lix", "deepseek-v4-pro"), str(ch))
check("pinned: цепочка без дублей", len(ch) == len(set(ch)), str(ch))
check("pinned: мёртвая тройка исключена",
      all(m not in DEAD_TRIO for _p, m in ch))
orch.is_cooldown = lambda k: k == "lix/deepseek-v4-pro"
ch2 = orch.chain_for_role_pinned(FAKE_CFG, "code", "lix/deepseek-v4-pro")
check("pinned: закреплённая в cooldown — не первая, а фолбэк",
      ch2 and ch2[0] != ("lix", "deepseek-v4-pro")
      and ("lix", "deepseek-v4-pro") not in ch2, str(ch2))
orch.is_cooldown = _real_cooldown
orch._router_dead = _real_dead

# --- 3. Извлечение разделов и отчёт ---------------------------------------
synth = ("## ВЕРДИКТ\nГодится, делать так.\n\n## ИТОГОВОЕ РЕШЕНИЕ\n"
         "Пишем так.\n\n## КОНФЛИКТЫ\nКодер и тестер спорят о скорости.\n\n"
         "## РИСКИ\nМало времени.\n")
check("extract: раздел ВЕРДИКТ",
      orch._extract_section(synth, "ВЕРДИКТ") == "Годится, делать так.")
check("extract: раздел КОНФЛИКТЫ",
      orch._extract_section(synth, "КОНФЛИКТЫ") == "Кодер и тестер спорят о скорости.")
check("extract: отсутствующий раздел пуст",
      orch._extract_section(synth, "НЕСУЩЕСТВУЕТ") == "")

res = [("coder", "lix/deepseek-v4-pro", True, {}),
       ("reviewer", "lix/glm-5.3-prime", True, {}),
       ("tester", "lix/kimi-k3", False, {})]
rep = orch.build_council_report("rid-1", "Сделать X", res, synth, True)
check("report: вердикт из синтеза в отчёте", "Годится, делать так." in rep)
check("report: конфликты из синтеза в отчёте",
      "Кодер и тестер спорят о скорости." in rep)
check("report: статусы всех ролей",
      "coder: lix/deepseek-v4-pro — ok" in rep
      and "tester: lix/kimi-k3 — FAILED" in rep)
rep_noconf = orch.build_council_report("rid-1", "Сделать X", res,
                                       "## ВЕРДИКТ\nОк.\n", True)
check("report: без раздела конфликтов — честная пометка",
      "не выделен" in rep_noconf)
rep_nosynth = orch.build_council_report("rid-1", "Сделать X", res, "", False)
check("report: синтез упал — конфликты неизвестны",
      "Неизвестны" in rep_nosynth and "Файлы" in rep_nosynth)

# --- 4. Dry-run end-to-end (без вызовов моделей) --------------------------
with tempfile.TemporaryDirectory() as tmp:
    wd = os.path.join(tmp, "c31")
    r = subprocess.run([sys.executable, "fenrir_orchestra.py", "council31",
                        "--task", "Напиши функцию is_prime",
                        "--workdir", wd, "--dry-run"],
                       capture_output=True, text=True, cwd=os.path.dirname(__file__))
    check("dry-run: exit 0", r.returncode == 0, r.stderr[-500:])
    for name in ("coder", "reviewer", "tester"):
        check(f"dry-run: workdir роли {name}",
              os.path.isfile(os.path.join(wd, name, "answer.md")))
        check(f"dry-run: файл роли {name}.md",
              os.path.isfile(os.path.join(wd, f"{name}.md")))
    check("dry-run: synthesis.md", os.path.isfile(os.path.join(wd, "synthesis.md")))
    check("dry-run: report.md с вердиктом",
          os.path.isfile(os.path.join(wd, "report.md"))
          and "Вердикт" in open(os.path.join(wd, "report.md"), encoding="utf-8").read())
    cj = json.load(open(os.path.join(wd, "council.json"), encoding="utf-8"))
    check("dry-run: council.json — 3 роли ok",
          cj.get("kind") == "council31"
          and len(cj.get("roles", [])) == 3
          and all(x["ok"] for x in cj["roles"]), str(cj.get("roles")))
    # задача из файла
    task_file = os.path.join(tmp, "task.txt")
    open(task_file, "w", encoding="utf-8").write("Задача из файла")
    wd2 = os.path.join(tmp, "c31-file")
    r2 = subprocess.run([sys.executable, "fenrir_orchestra.py", "council31",
                         "--file", task_file, "--workdir", wd2, "--dry-run"],
                        capture_output=True, text=True, cwd=os.path.dirname(__file__))
    check("dry-run: --file загружает задачу",
          r2.returncode == 0
          and "Задача из файла" in open(os.path.join(wd2, "task.json"),
                                        encoding="utf-8").read(), r2.stderr[-300:])
    r3 = subprocess.run([sys.executable, "fenrir_orchestra.py", "council31",
                         "--task", "", "--workdir", os.path.join(tmp, "c31-empty"),
                         "--dry-run"],
                        capture_output=True, text=True, cwd=os.path.dirname(__file__))
    check("dry-run: пустая задача — отказ (exit 2)", r3.returncode == 2)

# --- 5. Обёртка терминала: dry-run + запись в WORK_LOG --------------------
term = os.path.expanduser("~/workspace/fenrir-terminal")
with tempfile.TemporaryDirectory() as tmp:
    wl = os.path.join(tmp, "WORK_LOG.md")
    env = dict(os.environ, FENRIR_WORKLOG=wl)
    r = subprocess.run([sys.executable,
                        os.path.join(term, "fenrir_council.py"),
                        "--task", "Тест обёртки",
                        "--workdir", os.path.join(tmp, "c31w"),
                        "--dry-run"],
                       capture_output=True, text=True, env=env)
    check("wrapper: dry-run exit 0", r.returncode == 0, r.stderr[-500:])
    check("wrapper: строка в WORK_LOG",
          os.path.isfile(wl) and "консилиум v3.1" in
          open(wl, encoding="utf-8").read())

print(f"\nИТОГО: {len(PASSED)} пройдено, {len(FAILED)} провалено")
if FAILED:
    print("Провалены:", ", ".join(FAILED))
    sys.exit(1)
