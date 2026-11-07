#!/usr/bin/env python3
"""Fenrir Terminal v2.1 — роутер и экономика, ШАГ 1: роутер + учёт + алерты.

Только Python stdlib. Роутер только ВЫБИРАЕТ модель и СЧИТАЕТ токены.
Сам он вызовы моделей делает только для короткой health-пробы
("1+1=", max_tokens 20, последовательно, по одной модели за раз).
Живые задачи не запускает, serve не перезапускает, ключи не трогает.

Источники правды:
- providers.json — цепочки ролей code/review/text (порядок не меняется).
- state/cooldown.json — модели в cooldown (30 мин) пропускаются.
- state/prices.json — живой прайс и health (этот модуль пишет, atomic).
- runs/*/summary.json — учёт токенов за день (поле total_tokens).
- Артефакт капа 20 токенов: пустой ответ с finish_reason=length —
  живая модель, упёршаяся в кап, а не смерть (см. health_probe).

Правила Shadow (не нарушать):
- ТОЛЬКО Lix (приказ 06.11.2026 ~11:30). Klinch/Anymodel не используются.
- Мёртвая тройка (403, отсутствует в каталоге с 06.11 ~03:54):
  claude-opus-5.5, claude-sonnet-5.5, gpt-6-astra — НИКОГДА не предлагать.
- qwen-flash ЖИВ (проверен прямым вызовом 07.11 ~16:25) — не исключать:
  он кандидат для роли text, его health пробуется наравне с остальными.
- Lix — безлимит до 09.11.2026, множитель 0x: прайс ведётся, но деньги
  не считаются (input_per_1m/output_per_1m = 0). Дневной бюджет —
  мягкий сторож от разгона (10 млн токенов/день, предупреждение при 80%).

Использование:
    python3 fenrir_router.py pick --role code|review|text [--refresh]
    python3 fenrir_router.py status [--probe]
    python3 fenrir_router.py probe
    python3 fenrir_router.py refresh
Через терминал: fenrir router pick --role code | fenrir router status
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fenrir_orchestra as orch

try:
    from zoneinfo import ZoneInfo
    SAMARA = ZoneInfo("Europe/Samara")
except Exception:  # pragma: no cover - нет tzdata
    SAMARA = timezone(timedelta(hours=4))

BASE = orch.BASE
STATE_DIR = orch.STATE_DIR
RUNS_DIR = orch.RUNS_DIR
PRICES_PATH = os.path.join(STATE_DIR, "prices.json")
PRICES_BAK = PRICES_PATH + ".bak"
PRICES_LAST_VALID = PRICES_PATH + ".last_valid"
ALERT_PATH = os.path.join(STATE_DIR, "router-alert")
WORKLOG_PATH = os.path.join(BASE, "WORK_LOG.md")

# Мёртвая тройка: HTTP 403 даже на «1+1=», отсутствует в каталоге
# с 06.11.2026 ~03:54. Никогда не предлагать (приказ в задаче v2.1).
ROUTER_DEAD = {"claude-opus-5.5", "claude-sonnet-5.5", "gpt-6-astra"}
# qwen-flash жив (прямая проверка 07.11 ~16:25) — кандидат для text.
# В цепочках providers.json его нет, поэтому роутер добавляет сам.
EXTRA_TEXT_CANDIDATES = ["qwen-flash"]

PROBE_PROMPT = "1+1="
PROBE_MAX_TOKENS = 20
HEALTH_TTL_S = 30 * 60  # провал пробы подавляет модель в pick на 30 мин
DAILY_TOKENS_SOFT = 10_000_000  # мягкий дневной бюджет-сторож
WARN_AT = 0.8  # предупреждение при 80% бюджета
PRICES_VERSION = "2.1-step1"


# ---------------------------------------------------------------- utils
def _now_samara():
    return datetime.now(SAMARA)


def _today_samara():
    return _now_samara().date().isoformat()


def _log_event(actor, task, status, note):
    """Строка в WORK_LOG.md оркестры, с дедупом по точному тексту."""
    section = "## Роутер v2.1 — события\n"
    line = (f"- {_now_samara().strftime('%Y-%m-%d %H:%M')} | {actor} | "
            f"{task} | {status} | {note}\n")
    try:
        existing = ""
        if os.path.isfile(WORKLOG_PATH):
            with open(WORKLOG_PATH, encoding="utf-8") as f:
                existing = f.read()
        if line.strip() in existing:
            return False
        with open(WORKLOG_PATH, "a", encoding="utf-8") as f:
            if section not in existing:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("\n" + section + "\n")
            f.write(line)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- prices.json
def default_prices():
    cfg = orch.load_config()
    return {
        "version": PRICES_VERSION,
        "updated_at": None,
        "provider": "lix",
        "tariff": {
            "plan": "безлимит до 09.11.2026",
            "multiplier": "0x",
            "money_counted": False,
            "rpm": 260,
            "tpm": 2000000,
            "max_parallel": 3,
            "deadline": "2026-11-09T01:21:00+04:00",
        },
        "limits": {
            "daily_tokens_soft": DAILY_TOKENS_SOFT,
            "warn_at": WARN_AT,
        },
        "models": [],
        "task_profiles": {
            "code": (cfg["roles"].get("code") or ["lix/deepseek-v4.1-flash"])[0],
            "review": (cfg["roles"].get("review") or ["lix/glm-5.3-prime"])[0],
            "text": (cfg["roles"].get("text") or ["lix/kimi-k3"])[0],
        },
        "fallbacks": dict(cfg.get("roles", {})),
    }


def load_prices():
    """Читает prices.json, проверяет минимальную схему. Нет/битый -> None."""
    try:
        with open(PRICES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            return None
        return data
    except (OSError, ValueError):
        return None


def save_prices(data):
    """Атомарная запись: .bak (предыдущая) + .last_valid (последняя валидная)."""
    old = load_prices()
    if old is not None:
        try:
            orch.atomic_write_json(PRICES_BAK, old)
        except OSError:
            pass
    data["updated_at"] = _now_samara().isoformat(timespec="seconds")
    orch.atomic_write_json(PRICES_PATH, data)
    try:
        orch.atomic_write_json(PRICES_LAST_VALID, data)
    except OSError:
        pass
    return data


def dead_models(prices=None):
    """Мёртвые модели по prices.json; без файла — жёсткая тройка."""
    data = prices if prices is not None else load_prices()
    if data is None:
        return set(ROUTER_DEAD)
    dead = {m["id"] for m in data.get("models", [])
            if isinstance(m, dict) and m.get("enabled") is False}
    # Тройка мёртва всегда (403 с 06.11), даже если её нет в прайсе.
    return dead | set(ROUTER_DEAD)


def refresh_prices():
    """Живой каталог Lix -> prices.json. Денег нет (0x), health — отдельно."""
    cfg = orch.load_config()
    prov = cfg["providers"]["lix"]
    ok, ids, err = orch.list_models(prov)
    old = load_prices() or default_prices()
    old_lat = {m["id"]: m for m in old.get("models", []) if isinstance(m, dict)}
    data = default_prices()
    if not ok:
        data["models"] = old.get("models", [])
        data["catalog_ok"] = False
        data["catalog_error"] = err
        save_prices(data)
        return data, False, err
    models = []
    for mid in sorted(ids):
        prev = old_lat.get(mid, {})
        models.append({
            "id": mid,
            "provider": "lix",
            "input_per_1m": 0,
            "output_per_1m": 0,
            "context": None,
            "latency_s": prev.get("latency_s"),
            "last_probe_ok": prev.get("last_probe_ok"),
            "last_probe_at": prev.get("last_probe_at"),
            # Мёртв: в жёсткой тройке или нет в каталоге (каталог — источник).
            "enabled": mid not in ROUTER_DEAD,
        })
    data["models"] = models
    data["catalog_ok"] = True
    data["catalog_error"] = ""
    save_prices(data)
    return data, True, ""


# ---------------------------------------------------------------- health
def probe_targets():
    """Модели для health-пробы: головы цепочек + qwen-flash. Без дублей."""
    cfg = orch.load_config()
    out = []
    for role in ("code", "review", "text"):
        for spec in cfg["roles"].get(role, []):
            if spec.startswith("lix/"):
                model = spec.split("/", 1)[1]
                if model not in out:
                    out.append(model)
                break
    for m in EXTRA_TEXT_CANDIDATES:
        if m not in out:
            out.append(m)
    return out


def health_probe(models=None):
    """Короткая проба chat_once, последовательно. Cooldown НЕ ставится:
    провал пробы — запись в prices.json, а не приговор модели.
    ВАЖНО (артефакт капа, задокументирован 06.11.2026): при max_tokens=20
    некоторые живые модели дают пустой ответ с finish_reason=length —
    они успели сгенерировать токены и упёрлись в кап, это НЕ смерть.
    Мёртвая модель так не отвечает: у неё 403/429/502 или пусто со stop.
    Поэтому length+пусто считается alive (truncated)."""
    cfg = orch.load_config()
    prov = cfg["providers"]["lix"]
    targets = models or probe_targets()
    results = {}
    prices = load_prices() or default_prices()
    by_id = {m["id"]: m for m in prices.get("models", [])
             if isinstance(m, dict)}
    for model in targets:
        ok, content, meta, err = orch.chat_once(
            prov, model, PROBE_PROMPT, PROBE_MAX_TOKENS)
        if not ok and meta.get("finish_reason") == "length":
            # Жива, но упёрлась в кап 20 токенов (артефакт, не смерть).
            ok, content = True, "(truncated, alive)"
            err = "empty content (finish_reason=length) — alive, кап 20"
        results[model] = {
            "ok": ok,
            "content": content[:20] if ok else "",
            "finish_reason": meta.get("finish_reason"),
            "latency_s": meta.get("elapsed_s"),
            "error": err,
            "at": _now_samara().isoformat(timespec="seconds"),
        }
        entry = by_id.get(model)
        if entry is not None:
            entry["latency_s"] = meta.get("elapsed_s")
            entry["last_probe_ok"] = ok
            entry["last_probe_at"] = results[model]["at"]
            # Провал пробы живой модели — не смерть: enabled не трогаем.
            # Тройка и так disabled, остальные остаются enabled.
    save_prices(prices)
    return results


def probe_failed_recently(model, prices=None, now=None):
    """Провал пробы младше HEALTH_TTL_S — модель временно не берём в pick."""
    data = prices if prices is not None else load_prices()
    if data is None:
        return False
    now = now or time.time()
    for m in data.get("models", []):
        if isinstance(m, dict) and m.get("id") == model:
            if m.get("last_probe_ok") is False and m.get("last_probe_at"):
                try:
                    at = datetime.fromisoformat(m["last_probe_at"])
                    ts = at.timestamp()
                except ValueError:
                    return False
                return (now - ts) < HEALTH_TTL_S
            return False
    return False


# ---------------------------------------------------------------- pick
def candidates_for_role(role):
    """Цепочка роли из providers.json (+ qwen-flash для text). Порядок — как в конфиге."""
    cfg = orch.load_config()
    chain = list(cfg["roles"].get(role, cfg["roles"]["text"]))
    if role == "text":
        for m in EXTRA_TEXT_CANDIDATES:
            spec = f"lix/{m}"
            if spec not in chain:
                chain.append(spec)
    return chain


def pick(role):
    """Первая живая модель роли: не в cooldown, не мёртвая, без свежего
    провала пробы. Если все отфильтрованы — первая не-мёртвая (best effort)."""
    chain = candidates_for_role(role)
    cd = orch.load_cooldown()
    dead = dead_models()
    prices = load_prices()
    now = time.time()
    for spec in chain:
        pname, model = spec.split("/", 1)
        if f"{pname}/{model}" in cd:
            continue
        if model in dead:
            continue
        if probe_failed_recently(model, prices, now):
            continue
        return (pname, model), "ok"
    for spec in chain:
        pname, model = spec.split("/", 1)
        if model not in dead:
            return (pname, model), "best-effort: все живые отфильтрованы"
    return None, "нет живых моделей роли (все мёртвые)"


# ---------------------------------------------------------------- accounting
def today_tokens(date=None):
    """Токены за день (Самара) по runs/*/summary.json. Возвращает (tokens, runs)."""
    day = date or _today_samara()
    total = 0
    count = 0
    if not os.path.isdir(RUNS_DIR):
        return 0, 0
    for name in os.listdir(RUNS_DIR):
        sp = os.path.join(RUNS_DIR, name, "summary.json")
        if not os.path.isfile(sp):
            continue
        try:
            with open(sp, encoding="utf-8") as f:
                s = json.load(f)
            fa = s.get("finished_at", "")
            if fa.startswith(day):
                total += int(s.get("total_tokens", 0))
                count += 1
        except (OSError, ValueError, TypeError):
            continue
    return total, count


def budget_check(date=None):
    """Мягкий дневной бюджет: ok / warn (>=80%) / over (>=100%).
    Предупреждение — одна строка в WORK_LOG за день (дедуп по дате)."""
    tokens, runs = today_tokens(date)
    limit = DAILY_TOKENS_SOFT
    ratio = tokens / limit if limit else 0.0
    if ratio >= 1.0:
        status = "over"
    elif ratio >= WARN_AT:
        status = "warn"
    else:
        status = "ok"
    if status in ("warn", "over"):
        day = date or _today_samara()
        marker = os.path.join(STATE_DIR, f"router-budget-warned-{day}")
        if not os.path.exists(marker):
            _log_event("router", "дневной бюджет токенов", status,
                       f"{tokens}/{limit} токенов ({ratio:.0%}), прогонов {runs}")
            try:
                orch.atomic_write(marker, day + "\n")
            except OSError:
                pass
    return {"tokens": tokens, "runs": runs, "limit": limit,
            "ratio": round(ratio, 3), "status": status}


# ---------------------------------------------------------------- key-death alert
def key_health():
    """Жив ли ключ Lix: каталог + проба одной живой модели.
    Алерт — только если каталог недоступен И проба упала (не волна)."""
    cfg = orch.load_config()
    prov = cfg["providers"]["lix"]
    cat_ok, ids, cat_err = orch.list_models(prov)
    if cat_ok:
        clear_alert("ключ жив: каталог отвечает")
        return {"key_ok": True, "catalog_ok": True, "probe": None}
    # Каталог упал — перепроверяем живой моделью, прежде чем паниковать.
    live = None
    for spec in cfg["roles"]["text"]:
        if spec.startswith("lix/"):
            live = spec.split("/", 1)[1]
            break
    ok, content, meta, err = orch.chat_once(
        prov, live or "kimi-k3", PROBE_PROMPT, PROBE_MAX_TOKENS)
    probe = {"model": live, "ok": ok, "error": err,
             "http_status": meta.get("http_status")}
    if ok:
        clear_alert("ключ жив: каталог упал, но chat отвечает")
        return {"key_ok": True, "catalog_ok": False, "probe": probe}
    reason = f"каталог: {cat_err}; проба {live}: {err}"
    raise_alert(reason)
    return {"key_ok": False, "catalog_ok": False, "probe": probe,
            "reason": reason}


def raise_alert(reason):
    """Маркер state/router-alert + строка в WORK_LOG (один раз на причину)."""
    try:
        current = ""
        if os.path.isfile(ALERT_PATH):
            with open(ALERT_PATH, encoding="utf-8") as f:
                current = f.read()
        if reason in current:
            return False
        orch.atomic_write(ALERT_PATH, json.dumps({
            "at": _now_samara().isoformat(timespec="seconds"),
            "reason": reason,
        }, ensure_ascii=False) + "\n")
    except OSError:
        pass
    _log_event("router", "алерт: смерть ключа Lix", "failed", reason)
    return True


def clear_alert(note):
    """Ключ ожил: снять маркер, записать восстановление (если маркер был)."""
    if not os.path.isfile(ALERT_PATH):
        return False
    try:
        os.remove(ALERT_PATH)
    except OSError:
        pass
    _log_event("router", "алерт снят: ключ Lix жив", "ok", note)
    return True


def alert_state():
    if not os.path.isfile(ALERT_PATH):
        return None
    try:
        with open(ALERT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"at": None, "reason": "маркер не читается"}


# ---------------------------------------------------------------- CLI
def cmd_pick(args):
    if args.refresh:
        refresh_prices()
    got, why = pick(args.role)
    if got is None:
        print(f"router pick {args.role}: НЕТ живых моделей ({why})")
        return 1
    pname, model = got
    print(f"router pick {args.role}: {pname}/{model} ({why})")
    return 0


def cmd_status(args):
    if args.probe:
        results = health_probe()
        for m, r in results.items():
            print(f"  probe {m}: {'OK' if r['ok'] else 'FAIL ' + r['error']}"
                  f" ({r.get('latency_s')}s)")
    prices = load_prices()
    cd = orch.load_cooldown()
    cfg = orch.load_config()
    print("router status (только Lix):")
    print(f"  прайс: {'есть' if prices else 'НЕТ (запусти: fenrir router refresh)'}"
          f"{' от ' + str(prices.get('updated_at')) if prices else ''}")
    if prices:
        models = [m for m in prices.get("models", []) if isinstance(m, dict)]
        probed_ok = [m["id"] for m in models if m.get("last_probe_ok") is True]
        # Тройка 403 отсутствует в каталоге — она и есть «мёртвые»,
        # а не модели из каталога: в каталоге disabled нет никого.
        dead = sorted(dead_models(prices))
        print(f"  моделей в каталоге: {len(models)}, проба OK: {len(probed_ok)}"
              f" {probed_ok}")
        print(f"  мёртвые (403, вне каталога): {dead}")
        for m in prices.get("models", []):
            if not isinstance(m, dict):
                continue
            mid = m["id"]
            in_cd = f"lix/{mid}" in cd
            probe = m.get("last_probe_ok")
            probe_s = ("probe OK" if probe is True else
                       "probe FAIL" if probe is False else "probe —")
            print(f"    {mid:<22} {'dead' if not m.get('enabled') else 'live':<4} "
                  f"{'cooldown' if in_cd else '':<9} {probe_s}")
    tokens, runs = today_tokens()
    budget = budget_check()
    print(f"  токены сегодня: {tokens} ({runs} прогонов), "
          f"бюджет {budget['limit']} ({budget['ratio']:.0%}) — {budget['status']}")
    print(f"  дедлайн Lix: {orch.lix_deadline_remaining(cfg)}")
    alert = alert_state()
    print(f"  алерт ключа: {'ЕСТЬ: ' + str(alert.get('reason')) if alert else 'нет'}")
    return 0


def cmd_probe(_args):
    results = health_probe()
    for m, r in results.items():
        print(f"probe {m}: {'OK ' + repr(r['content']) if r['ok'] else 'FAIL ' + r['error']}"
              f" ({r.get('latency_s')}s)")
    return 0 if any(r["ok"] for r in results.values()) else 1


def cmd_refresh(_args):
    data, ok, err = refresh_prices()
    if ok:
        print(f"prices.json обновлён: {len(data['models'])} моделей в каталоге, "
              f"мёртвых: {sorted(dead_models(data))}")
        return 0
    print(f"refresh FAILED: {err} (старый прайс сохранён)")
    return 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="fenrir_router")
    sub = p.add_subparsers(dest="cmd", required=True)
    pk = sub.add_parser("pick")
    pk.add_argument("--role", default="code", choices=["code", "review", "text"])
    pk.add_argument("--refresh", action="store_true")
    pk.set_defaults(fn=cmd_pick)
    st = sub.add_parser("status")
    st.add_argument("--probe", action="store_true")
    st.set_defaults(fn=cmd_status)
    pr = sub.add_parser("probe")
    pr.set_defaults(fn=cmd_probe)
    rf = sub.add_parser("refresh")
    rf.set_defaults(fn=cmd_refresh)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
