#!/usr/bin/env python3
"""Тесты роутера v2.1 шаг 1 (fenrir_router.py + встройка chain_for_role).

Живые вызовы не делаются: list_models/chat_once/load_config/load_cooldown
подменяются стабами. Запуск: python3 test_router.py
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
    tmp = tempfile.mkdtemp(prefix="router-test-")
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
    router.WORKLOG_PATH = os.path.join(tmp, "WORK_LOG.md")
    orch.STATE_DIR = state
    return tmp


def stub_config():
    orch.load_config = lambda: FAKE_CFG
    orch.load_cooldown = lambda: {}


def test_pick_skips_cooldown_and_dead():
    setup_tmp()
    stub_config()
    orch.load_cooldown = lambda: {"lix/deepseek-v4.1-flash": 9999999999}
    got, why = router.pick("code")
    check("pick/code пропускает cooldown", got == ("lix", "kimi-k3"), str(got))
    # Все в cooldown -> best-effort: первая не-мёртвая
    orch.load_cooldown = lambda: {
        "lix/deepseek-v4.1-flash": 9999999999,
        "lix/kimi-k3": 9999999999,
        "lix/qwen3.8-max": 9999999999,
        "lix/deepseek-v4-pro": 9999999999,
    }
    got, why = router.pick("code")
    check("pick/code best-effort при всех в cooldown",
          got == ("lix", "deepseek-v4.1-flash") and "best-effort" in why, str(got))


def test_pick_text_has_qwen_flash():
    setup_tmp()
    stub_config()
    cands = router.candidates_for_role("text")
    check("text: qwen-flash добавлен кандидатом",
          "lix/qwen-flash" in cands, str(cands))
    # qwen-flash не мёртвый и не в cooldown — берётся, когда цепочка занята
    orch.load_cooldown = lambda: {"lix/kimi-k3": 9999999999,
                                  "lix/deepseek-v4.1-flash": 9999999999}
    got, _ = router.pick("text")
    check("pick/text доходит до qwen-flash", got == ("lix", "qwen-flash"), str(got))


def test_pick_skips_dead_trio_and_failed_probe():
    setup_tmp()
    stub_config()
    # Мёртвая тройка в цепочке (гипотетически) никогда не берётся
    cfg = dict(FAKE_CFG)
    cfg["roles"] = dict(FAKE_CFG["roles"])
    cfg["roles"]["code"] = ["lix/claude-opus-5.5", "lix/kimi-k3"]
    orch.load_config = lambda: cfg
    got, _ = router.pick("code")
    check("pick/code пропускает мёртвую тройку", got == ("lix", "kimi-k3"), str(got))
    # Свежий провал пробы подавляет модель
    stub_config()
    orch.atomic_write_json(router.PRICES_PATH, {
        "version": "x", "models": [
            {"id": "kimi-k3", "enabled": True, "last_probe_ok": False,
             "last_probe_at": router._now_samara().isoformat()},
            {"id": "deepseek-v4.1-flash", "enabled": True},
        ]})
    got, _ = router.pick("code")
    check("pick/code пропускает свежий провал пробы",
          got == ("lix", "deepseek-v4.1-flash"), str(got))


def test_pick_no_network():
    setup_tmp()
    stub_config()

    def _boom(*a, **k):
        raise AssertionError("pick не должен ходить в сеть")
    orch.list_models = _boom
    orch.chat_once = _boom
    got, _ = router.pick("review")
    check("pick не делает сетевых вызовов", got == ("lix", "glm-5.3-prime"), str(got))


def test_prices_refresh_and_dead():
    setup_tmp()
    stub_config()
    orch.list_models = lambda p: (True, [
        "deepseek-v4.1-flash", "kimi-k3", "qwen-flash",
        "claude-opus-5.5", "gpt-6-astra"], "")
    data, ok, err = router.refresh_prices()
    check("refresh: каталог принят", ok and len(data["models"]) == 5, err)
    by_id = {m["id"]: m for m in data["models"]}
    check("refresh: тройка disabled",
          by_id["claude-opus-5.5"]["enabled"] is False
          and by_id["gpt-6-astra"]["enabled"] is False)
    check("refresh: qwen-flash enabled",
          by_id["qwen-flash"]["enabled"] is True)
    check("refresh: цены 0 (безлимит 0x)",
          by_id["kimi-k3"]["input_per_1m"] == 0
          and by_id["kimi-k3"]["output_per_1m"] == 0)
    check("refresh: файл на диске", os.path.isfile(router.PRICES_PATH))
    check("refresh: last_valid создан",
          os.path.isfile(router.PRICES_LAST_VALID))
    # Повторный refresh: .bak — предыдущая версия
    orch.list_models = lambda p: (True, ["kimi-k3"], "")
    router.refresh_prices()
    check("refresh: .bak хранит предыдущий прайс",
          os.path.isfile(router.PRICES_BAK))
    check("dead_models: тройка из прайса (всегда мёртва)",
          router.dead_models() == router.ROUTER_DEAD)
    # Битый прайс -> load None, dead -> тройка
    with open(router.PRICES_PATH, "w", encoding="utf-8") as f:
        f.write("{не json")
    check("load_prices: битый файл -> None", router.load_prices() is None)
    check("dead_models: без прайса -> тройка",
          router.dead_models() == router.ROUTER_DEAD)


def test_today_tokens_and_budget():
    tmp = setup_tmp()
    stub_config()
    day = router._today_samara()
    for name, tokens, fa in [
            ("r1", 1000, day + "T10:00:00+00:00"),
            ("r2", 2500, day + "T11:00:00+00:00"),
            ("r3", 9999, "2026-11-01T10:00:00+00:00")]:
        d = os.path.join(router.RUNS_DIR, name)
        os.makedirs(d)
        with open(os.path.join(d, "summary.json"), "w", encoding="utf-8") as f:
            json.dump({"total_tokens": tokens, "finished_at": fa}, f)
    total, count = router.today_tokens()
    check("today_tokens: сумма только за сегодня",
          total == 3500 and count == 2, f"{total}/{count}")
    # Бюджет: warn при 80% от 10 млн
    router.today_tokens = lambda date=None: (8_500_000, 40)
    b = router.budget_check()
    check("budget: 8.5M -> warn", b["status"] == "warn", str(b))
    check("budget: предупреждение в WORK_LOG",
          os.path.isfile(router.WORKLOG_PATH)
          and "бюджет" in open(router.WORKLOG_PATH, encoding="utf-8").read())
    # Дедуп: второй вызов в тот же день строку не добавляет
    before = open(router.WORKLOG_PATH, encoding="utf-8").read()
    router.budget_check()
    after = open(router.WORKLOG_PATH, encoding="utf-8").read()
    check("budget: дедуп предупреждения за день", before == after)
    router.today_tokens = lambda date=None: (100, 1)
    b = router.budget_check()
    check("budget: 100 токенов -> ok", b["status"] == "ok", str(b))


def test_alert_raise_clear():
    setup_tmp()
    stub_config()
    check("alert: сначала нет", router.alert_state() is None)
    router.raise_alert("каталог: HTTP 403; проба kimi-k3: HTTP 403")
    check("alert: маркер создан", os.path.isfile(router.ALERT_PATH))
    check("alert: причина в маркере",
          "HTTP 403" in open(router.ALERT_PATH, encoding="utf-8").read())
    wl = open(router.WORKLOG_PATH, encoding="utf-8").read()
    check("alert: строка в WORK_LOG", "алерт" in wl)
    router.raise_alert("каталог: HTTP 403; проба kimi-k3: HTTP 403")
    wl2 = open(router.WORKLOG_PATH, encoding="utf-8").read()
    check("alert: дедуп той же причины", wl == wl2)
    router.clear_alert("ключ жив")
    check("alert: маркер снят", not os.path.exists(router.ALERT_PATH))
    wl3 = open(router.WORKLOG_PATH, encoding="utf-8").read()
    check("alert: восстановление залогировано", "алерт снят" in wl3)


def test_chain_for_role_uses_router_dead():
    tmp = setup_tmp()
    stub_config()
    # prices.json: kimi-k3 disabled -> цепочка его пропускает
    orch.atomic_write_json(os.path.join(tmp, "state", "prices.json"), {
        "version": "x", "models": [
            {"id": "kimi-k3", "enabled": False},
            {"id": "deepseek-v4.1-flash", "enabled": True},
        ]})
    chain = orch.chain_for_role(FAKE_CFG, "code")
    check("chain_for_role: пропускает disabled из prices.json",
          ("lix", "kimi-k3") not in chain
          and ("lix", "deepseek-v4.1-flash") in chain, str(chain))
    # Без prices.json — тройка всё равно пропускается
    os.remove(os.path.join(tmp, "state", "prices.json"))
    cfg = dict(FAKE_CFG)
    cfg["roles"] = dict(FAKE_CFG["roles"])
    cfg["roles"]["code"] = ["lix/claude-opus-5.5", "lix/kimi-k3"]
    chain = orch.chain_for_role(cfg, "code")
    check("chain_for_role: без прайса тройка пропускается",
          chain == [("lix", "kimi-k3")], str(chain))
    # qwen-flash в цепочке не пропускается (он жив)
    cfg["roles"]["code"] = ["lix/qwen-flash", "lix/kimi-k3"]
    chain = orch.chain_for_role(cfg, "code")
    check("chain_for_role: qwen-flash не исключается",
          ("lix", "qwen-flash") in chain, str(chain))


def test_health_probe_records():
    setup_tmp()
    stub_config()
    orch.list_models = lambda p: (True, ["kimi-k3", "qwen-flash"], "")
    router.refresh_prices()

    def fake_chat(prov, model, prompt, max_tokens):
        if model == "kimi-k3":
            return True, "2", {"finish_reason": "stop", "elapsed_s": 1.2}, ""
        return False, "", {"elapsed_s": 0.5}, "HTTP 502"
    orch.chat_once = fake_chat
    results = router.health_probe(models=["kimi-k3", "qwen-flash"])
    check("probe: ok и fail записаны",
          results["kimi-k3"]["ok"] is True
          and results["qwen-flash"]["ok"] is False, str(results))
    prices = router.load_prices()
    by_id = {m["id"]: m for m in prices["models"]}
    check("probe: last_probe_ok в прайсе",
          by_id["kimi-k3"]["last_probe_ok"] is True
          and by_id["qwen-flash"]["last_probe_ok"] is False)
    check("probe: провал НЕ делает модель disabled",
          by_id["qwen-flash"]["enabled"] is True)


def test_health_probe_length_is_alive():
    """Артефакт капа 20: пусто + finish_reason=length — живая модель."""
    setup_tmp()
    stub_config()
    orch.list_models = lambda p: (True, ["kimi-k3", "deepseek-v4.1-flash"], "")
    router.refresh_prices()

    def fake_chat(prov, model, prompt, max_tokens):
        if model == "kimi-k3":
            return (False, "", {"finish_reason": "length", "elapsed_s": 3.5},
                    "empty content (finish_reason=length)")
        return False, "", {"finish_reason": "stop", "elapsed_s": 0.5}, \
            "empty content (finish_reason=stop)"
    orch.chat_once = fake_chat
    results = router.health_probe(models=["kimi-k3", "deepseek-v4.1-flash"])
    check("probe: length+пусто считается alive",
          results["kimi-k3"]["ok"] is True, str(results))
    check("probe: stop+пусто считается fail",
          results["deepseek-v4.1-flash"]["ok"] is False, str(results))
    prices = router.load_prices()
    by_id = {m["id"]: m for m in prices["models"]}
    check("probe: length записан как last_probe_ok=True",
          by_id["kimi-k3"]["last_probe_ok"] is True)
    # И pick после такой пробы модель не пропускает
    got, _ = router.pick("text")
    check("pick: модель после length-пробы берётся",
          got == ("lix", "kimi-k3"), str(got))


def main():
    test_pick_skips_cooldown_and_dead()
    test_pick_text_has_qwen_flash()
    test_pick_skips_dead_trio_and_failed_probe()
    test_pick_no_network()
    test_prices_refresh_and_dead()
    test_today_tokens_and_budget()
    test_alert_raise_clear()
    test_chain_for_role_uses_router_dead()
    test_health_probe_records()
    test_health_probe_length_is_alive()
    print(f"\nИТОГО: {len(PASSED)} пройдено, {len(FAILED)} провалено")
    if FAILED:
        print("Провалы:", ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
