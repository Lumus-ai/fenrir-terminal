#!/usr/bin/env python3
"""Тесты роутера v2.1 шаг 2 (fenrir_router.py: weekly + бюджет-фолбэк).

Живые вызовы не делаются: load_config/load_cooldown/today_tokens
подменяются стабами, runs — синтетические. Запуск: python3 test_router_weekly.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fenrir_orchestra as orch
import fenrir_router as router

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

PASSED = []
FAILED = []


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("OK  " if cond else "FAIL") + f" {name} {extra}")


def setup_tmp():
    tmp = tempfile.mkdtemp(prefix="router-weekly-test-")
    state = os.path.join(tmp, "state")
    runs = os.path.join(tmp, "runs")
    os.makedirs(state)
    os.makedirs(runs)
    router.STATE_DIR = state
    router.RUNS_DIR = runs
    router.PRICES_PATH = os.path.join(state, "prices.json")
    router.PRICES_BAK = router.PRICES_PATH + ".bak"
    router.PRICES_LAST_VALID = router.PRICES_PATH + ".last_valid"
    router.ALERT_PATH = os.path.join(state, "router-alert")
    router.BUDGET_MARKER = os.path.join(state, "router-budget")
    router.REPORTS_DIR = os.path.join(tmp, "reports")
    router.WORKLOG_PATH = os.path.join(tmp, "WORK_LOG.md")
    orch.STATE_DIR = state
    orch.load_config = lambda: FAKE_CFG
    orch.load_cooldown = lambda: {}
    return tmp


def write_run(name, summary, metas=None):
    d = os.path.join(router.RUNS_DIR, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f)
    for task_dir, meta in (metas or {}).items():
        td = os.path.join(d, task_dir)
        os.makedirs(td, exist_ok=True)
        with open(os.path.join(td, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)


def write_prices(models):
    orch.atomic_write_json(router.PRICES_PATH, {
        "version": "2.1-step2", "provider": "lix",
        "tariff": {"money_counted": False},
        "limits": {"daily_tokens_soft": router.DAILY_TOKENS_SOFT},
        "models": models})


def test_weekly_synthetic():
    setup_tmp()
    # Неделя 2026-11-01..07 (конец — суббота, ISO W45). finished_at — UTC,
    # 10:00 UTC = 14:00 Самара, тот же календарный день.
    write_run("r1", {
        "total": 2, "ok": 2, "failed": 0, "total_tokens": 1000,
        "prompt_tokens": 200, "completion_tokens": 800, "requests": 2,
        "elapsed_s": 10.0, "finished_at": "2026-11-01T10:00:00+00:00",
        "tasks": [
            {"idx": 1, "role": "code", "ok": True, "provider": "lix",
             "model": "kimi-k3", "total_tokens": 600, "prompt_tokens": 100,
             "completion_tokens": 500, "attempts": 1},
            {"idx": 2, "role": "code", "ok": True, "provider": "lix",
             "model": "kimi-k3", "total_tokens": 400, "prompt_tokens": 100,
             "completion_tokens": 300, "attempts": 1}]})
    write_run("r2", {
        "total": 3, "ok": 1, "failed": 2, "total_tokens": 500,
        "prompt_tokens": 100, "completion_tokens": 400, "requests": 5,
        "elapsed_s": 30.0, "finished_at": "2026-11-03T10:00:00+00:00",
        "tasks": [
            {"idx": 1, "role": "code", "ok": True, "provider": "lix",
             "model": "deepseek-v4-pro", "total_tokens": 500,
             "prompt_tokens": 100, "completion_tokens": 400, "attempts": 2},
            {"idx": 2, "role": "code", "ok": False, "provider": None,
             "model": None, "total_tokens": 0, "attempts": 2},
            {"idx": 3, "role": "code", "ok": False, "provider": None,
             "model": None, "total_tokens": 0, "attempts": 0}]},
        metas={"task-2": {"attempts": [
            {"error": "HTTP 524"}, {"error": "HTTP 429"}]}})
    write_run("r3", {
        "total": 1, "ok": 1, "failed": 0, "total_tokens": 100,
        "prompt_tokens": 50, "completion_tokens": 50, "requests": 1,
        "elapsed_s": 5.0, "finished_at": "2026-11-07T10:00:00+00:00",
        "tasks": [
            {"idx": 1, "role": "text", "ok": True, "provider": "lix",
             "model": "qwen-flash", "total_tokens": 100, "prompt_tokens": 50,
             "completion_tokens": 50, "attempts": 1}]})
    # Вне недели (октябрь) — не считается; без даты — skipped.
    write_run("r0", {"total": 1, "ok": 1, "failed": 0, "total_tokens": 999,
                     "finished_at": "2026-10-25T10:00:00+00:00", "tasks": []})
    write_run("rX", {"total": 1, "ok": 1, "failed": 0, "total_tokens": 999,
                     "tasks": []})
    data = router.collect_week("2026-11-07")
    t = data["totals"]
    check("weekly: итог токенов/задач",
          t["tokens"] == 1600 and t["tasks"] == 6
          and t["ok"] == 4 and t["failed"] == 2
          and t["runs"] == 3 and t["requests"] == 8, str(t))
    check("weekly: по дням",
          data["per_day"]["2026-11-01"]["tokens"] == 1000
          and data["per_day"]["2026-11-03"]["tokens"] == 500
          and data["per_day"]["2026-11-07"]["tokens"] == 100
          and data["per_day"]["2026-11-02"]["runs"] == 0, str(data["per_day"]))
    check("weekly: по моделям",
          data["per_model"]["kimi-k3"]["tokens"] == 1000
          and data["per_model"]["deepseek-v4-pro"]["tokens"] == 500
          and data["per_model"]["qwen-flash"]["tokens"] == 100,
          str(data["per_model"]))
    check("weekly: топ-3",
          [m for m, _, _ in data["top_models"]] ==
          ["kimi-k3", "deepseek-v4-pro", "qwen-flash"], str(data["top_models"]))
    check("weekly: стоимость 0 (Lix 0x)",
          data["cost"] == 0 and data["money_counted"] is False)
    check("weekly: инциденты (последняя ошибка 429 + 0 попыток)",
          data["incidents"].get("HTTP 429 (параллельный лимит)") == 1
          and data["incidents"].get("0 попыток (не запущена)") == 1,
          str(data["incidents"]))
    check("weekly: skipped без даты", data["skipped"] == 1, str(data["skipped"]))
    check("weekly: средняя латентность",
          data["avg_latency_s"] == 15.0, str(data["avg_latency_s"]))
    check("weekly: ISO-неделя W45", data["iso_week"] == "2026-W45",
          data["iso_week"])
    check("weekly: макс. бюджет дня",
          data["max_ratio"] == 0.0 or data["max_ratio"] < 0.01,
          str(data["max_ratio"]))
    path, _ = router.weekly_report("2026-11-07")
    check("weekly: файл weekly-2026-W45.md",
          path.endswith("reports/weekly-2026-W45.md")
          and os.path.isfile(path), path)
    text = open(path, encoding="utf-8").read()
    check("weekly: в файле таблицы дней/моделей/инцидентов",
          "| 2026-11-01 |" in text and "| kimi-k3 |" in text
          and "HTTP 429" in text and "0 попыток" in text)
    check("weekly: строка в WORK_LOG",
          "еженедельный отчёт" in open(router.WORKLOG_PATH,
                                        encoding="utf-8").read())


def test_weekly_empty():
    setup_tmp()
    data = router.collect_week("2026-11-07")
    check("weekly пусто: нули без падения",
          data["totals"]["runs"] == 0 and data["totals"]["tokens"] == 0
          and data["top_models"] == [] and data["avg_latency_s"] is None)
    path, _ = router.weekly_report("2026-11-07")
    check("weekly пусто: файл создан", os.path.isfile(path))
    text = open(path, encoding="utf-8").read()
    check("weekly пусто: 'все задачи ok' в инцидентах",
          "все задачи за неделю ok" in text)


def test_classify_error():
    check("classify: 429", router._classify_error("x HTTP 429 y").startswith("HTTP 429"))
    check("classify: 403", router._classify_error("HTTP 403").startswith("HTTP 403"))
    check("classify: 502", router._classify_error("HTTP 502").startswith("HTTP 502"))
    check("classify: length",
          router._classify_error("empty content (finish_reason=length)") ==
          "пустой ответ (length, кап токенов)")
    check("classify: пустая ошибка", router._classify_error("") == "без ошибки")


def test_fallback_cheapest():
    setup_tmp()
    write_prices([
        {"id": "deepseek-v4.1-flash", "enabled": True,
         "input_per_1m": 6, "output_per_1m": 4, "latency_s": 2.0},
        {"id": "kimi-k3", "enabled": True,
         "input_per_1m": 1, "output_per_1m": 0, "latency_s": 5.0},
        {"id": "qwen3.8-max", "enabled": True,
         "input_per_1m": 1, "output_per_1m": 0, "latency_s": 1.0},
        {"id": "deepseek-v4-pro", "enabled": True,
         "input_per_1m": 20, "output_per_1m": 20, "latency_s": 3.0},
    ])
    # Бюджет 85% -> warn: фолбэк на самую дешёвую живую (цена 1, латентность 1.0)
    router.today_tokens = lambda date=None: (8_500_000, 40)
    got, why = router.pick("code")
    check("fallback 85%: самая дешёвая живая (qwen3.8-max)",
          got == ("lix", "qwen3.8-max") and "budget-fallback" in why, str(got) + why)
    marker = router.budget_marker_state()
    check("fallback: маркер state/router-budget",
          marker and marker["status"] == "warn"
          and marker["model"] == "qwen3.8-max", str(marker))
    wl = open(router.WORKLOG_PATH, encoding="utf-8").read()
    check("fallback: строка в WORK_LOG",
          "бюджет-фолбэк включён" in wl, wl[-200:])
    # Повторный pick — дедуп строки
    router.pick("code")
    wl2 = open(router.WORKLOG_PATH, encoding="utf-8").read()
    check("fallback: дедуп строки за день",
          wl2.count("бюджет-фолбэк включён") == 1)
    # Дешёвая в cooldown — следующая по цене/латентности (kimi-k3)
    orch.load_cooldown = lambda: {"lix/qwen3.8-max": 9999999999}
    got, _ = router.pick("code")
    check("fallback: дешёвая в cooldown — следующая (kimi-k3)",
          got == ("lix", "kimi-k3"), str(got))
    # Бюджет 100%+ -> over, тоже фолбэк
    orch.load_cooldown = lambda: {}
    router.today_tokens = lambda date=None: (12_000_000, 50)
    got, why = router.pick("code")
    check("fallback 120%: over тоже фолбэк",
          got == ("lix", "qwen3.8-max") and "budget-fallback" in why, str(got))
    marker = router.budget_marker_state()
    check("fallback: маркер over", marker and marker["status"] == "over", str(marker))


def test_fallback_cleared_new_day():
    setup_tmp()
    write_prices([
        {"id": "deepseek-v4.1-flash", "enabled": True,
         "input_per_1m": 1, "output_per_1m": 0, "latency_s": 2.0},
        {"id": "kimi-k3", "enabled": True,
         "input_per_1m": 1, "output_per_1m": 0, "latency_s": 5.0},
    ])
    # Маркер вчерашнего дня + бюджет сегодня ok -> снят
    orch.atomic_write(router.BUDGET_MARKER, json.dumps({
        "date": "2026-11-06", "status": "warn", "ratio": 0.85,
        "tokens": 8500000, "limit": 10000000, "role": "code",
        "model": "kimi-k3", "at": "2026-11-06T20:00:00+04:00"}))
    router.today_tokens = lambda date=None: (100, 1)
    got, why = router.pick("code")
    check("fallback: новый день снимает маркер",
          not os.path.exists(router.BUDGET_MARKER), str(router.budget_marker_state()))
    check("fallback: после снятия — обычный pick",
          got == ("lix", "deepseek-v4.1-flash") and why == "ok", str(got) + why)
    wl = open(router.WORKLOG_PATH, encoding="utf-8").read()
    check("fallback: снятие залогировано", "бюджет-фолбэк снят" in wl)
    # Бюджет ok без маркера — pick не пишет ничего лишнего
    before = open(router.WORKLOG_PATH, encoding="utf-8").read()
    router.pick("code")
    after = open(router.WORKLOG_PATH, encoding="utf-8").read()
    check("fallback: ok без маркера — WORK_LOG не растёт", before == after)


def test_fallback_no_prices():
    """Без prices.json цены равны — решает порядок цепочки."""
    setup_tmp()
    router.today_tokens = lambda date=None: (9_000_000, 40)
    got, why = router.pick("code")
    check("fallback без прайса: первая живая цепочки",
          got == ("lix", "deepseek-v4.1-flash") and "budget-fallback" in why,
          str(got) + why)


def main():
    test_weekly_synthetic()
    test_weekly_empty()
    test_classify_error()
    test_fallback_cheapest()
    test_fallback_cleared_new_day()
    test_fallback_no_prices()
    print(f"\nИТОГО: {len(PASSED)} пройдено, {len(FAILED)} провалено")
    if FAILED:
        print("Провалы:", ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
