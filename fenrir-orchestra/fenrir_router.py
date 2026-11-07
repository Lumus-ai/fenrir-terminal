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
    python3 fenrir_router.py weekly [--date YYYY-MM-DD]
Через терминал: fenrir router pick --role code | fenrir router status
    | fenrir router weekly

Шаг 2: еженедельный отчёт (reports/weekly-YYYY-Www.md: токены по дням
и моделям, топ-3, инциденты из meta.json, стоимость 0 при Lix 0x)
и фолбэк при бюджете: при warn/over pick берёт самую дешёвую живую
модель роли (цена, затем латентность), маркер state/router-budget,
снимается при сбросе дня или возврате бюджета в ok.
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
BUDGET_MARKER = os.path.join(STATE_DIR, "router-budget")
REPORTS_DIR = os.path.join(BASE, "reports")
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
PRICES_VERSION = "2.1-step2"
MAX_META_READS_WEEKLY = 500  # кап чтения meta.json при сборе инцидентов


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


def cheapest_live_for_role(role, prices=None, cooldown=None, now=None):
    """Самая дешёвая живая модель роли: сортировка по (цена, латентность,
    порядок цепочки). Цены сейчас все 0 (Lix 0x) — решает латентность
    из probes, при её отсутствии — порядок цепочки. Живая = не в
    cooldown, не мёртвая, без свежего провала пробы."""
    chain = candidates_for_role(role)
    cd = cooldown if cooldown is not None else orch.load_cooldown()
    dead = dead_models(prices)
    data = prices if prices is not None else load_prices()
    by_id = {}
    if data is not None:
        by_id = {m["id"]: m for m in data.get("models", [])
                 if isinstance(m, dict)}
    now = now or time.time()
    scored = []
    for idx, spec in enumerate(chain):
        pname, model = spec.split("/", 1)
        if f"{pname}/{model}" in cd:
            continue
        if model in dead:
            continue
        if probe_failed_recently(model, data, now):
            continue
        entry = by_id.get(model, {})
        price = (entry.get("input_per_1m") or 0) + (entry.get("output_per_1m") or 0)
        lat = entry.get("latency_s")
        scored.append(((price, lat if isinstance(lat, (int, float)) else float("inf"),
                        idx), (pname, model)))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0])
    return scored[0][1]


def budget_marker_state():
    """Текущий маркер фолбэка по бюджету (state/router-budget) или None."""
    if not os.path.isfile(BUDGET_MARKER):
        return None
    try:
        with open(BUDGET_MARKER, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return {"date": None, "status": "unreadable"}


def sync_budget_marker(budget, role=None, model=None):
    """Маркер фолбэка: ставится при warn/over, снимается при ok или
    наступлении нового дня. Строка в WORK_LOG — один раз на
    (день, статус) при установке и один раз при снятии."""
    today = budget.get("date") or _today_samara()
    current = budget_marker_state()
    # Протухший маркер (другой день) — снять, затем обработать сегодня.
    if current is not None and current.get("date") not in (None, today):
        try:
            os.remove(BUDGET_MARKER)
        except OSError:
            pass
        _log_event("router", "бюджет-фолбэк снят", "ok",
                   f"новый день {today}, старый маркер {current.get('date')}")
        current = None
    status = budget["status"]
    if status == "ok":
        if current is not None:
            try:
                os.remove(BUDGET_MARKER)
            except OSError:
                pass
            _log_event("router", "бюджет-фолбэк снят", "ok",
                       f"бюджет дня {today}: {budget['tokens']}/{budget['limit']} "
                       f"({budget['ratio']:.0%})")
        return None
    if status in ("warn", "over"):
        new = {"date": today, "status": status, "ratio": budget["ratio"],
               "tokens": budget["tokens"], "limit": budget["limit"],
               "role": role, "model": model,
               "at": _now_samara().isoformat(timespec="seconds")}
        if (current and current.get("date") == today
                and current.get("status") == status
                and current.get("model") == model):
            return current
        try:
            orch.atomic_write(BUDGET_MARKER, json.dumps(new, ensure_ascii=False) + "\n")
        except OSError:
            pass
        _log_event("router", "бюджет-фолбэк включён", status,
                   f"{budget['tokens']}/{budget['limit']} токенов "
                   f"({budget['ratio']:.0%}), роль {role} -> {model}")
        return new
    return current


def pick(role):
    """Первая живая модель роли: не в cooldown, не мёртвая, без свежего
    провала пробы. Если все отфильтрованы — первая не-мёртвая (best effort).
    Шаг 2: при warn/over дневного бюджета — самая дешёвая живая модель
    роли (фолбэк), маркер state/router-budget."""
    chain = candidates_for_role(role)
    cd = orch.load_cooldown()
    dead = dead_models()
    prices = load_prices()
    now = time.time()
    budget = budget_check()
    if budget["status"] in ("warn", "over"):
        got = cheapest_live_for_role(role, prices, cd, now)
        if got is not None:
            sync_budget_marker(budget, role, got[1])
            return got, (f"budget-fallback: {budget['ratio']:.0%} дневного "
                         f"бюджета ({budget['tokens']}/{budget['limit']})")
        sync_budget_marker(budget, role, None)
    else:
        sync_budget_marker(budget)
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


# ---------------------------------------------------------------- weekly report
def _samara_date(iso):
    """Дата (Самара) из finished_at (UTC ISO) или None при битом значении.
    Шаг 1 today_tokens() сравнивает префикс UTC-строки с самарской датой —
    здесь честная конвертация: прогон в 00:00–04:00 по Самаре относится
    к самарскому, а не к UTC-дню."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SAMARA).date()


def _classify_error(err):
    """Класс инцидента по строке ошибки из meta.json attempts."""
    e = str(err or "")
    if "HTTP 429" in e:
        return "HTTP 429 (параллельный лимит)"
    if "HTTP 403" in e:
        return "HTTP 403 (мёртвая модель)"
    if "HTTP 502" in e:
        return "HTTP 502"
    if "HTTP 524" in e:
        return "HTTP 524 (таймаут провайдера)"
    if "finish_reason=length" in e:
        return "пустой ответ (length, кап токенов)"
    if "empty content" in e:
        return "пустой ответ (другое)"
    if e.strip():
        return "прочее: " + e[:60]
    return "без ошибки"


def collect_week(end_date=None):
    """Агрегация runs/*/summary.json за 7 дней до end_date включительно
    (даты — Самара). Возвращает dict с итогами по дням/моделям,
    инцидентами и латентностью. Денег нет: Lix 0x, cost всегда 0."""
    if end_date is None:
        end = _now_samara().date()
    elif isinstance(end_date, str):
        end = datetime.fromisoformat(end_date).date()
    else:
        end = end_date
    days = [(end - timedelta(days=i)) for i in range(6, -1, -1)]
    day_keys = [d.isoformat() for d in days]
    per_day = {k: {"runs": 0, "tasks": 0, "ok": 0, "failed": 0,
                   "tokens": 0, "prompt": 0, "completion": 0,
                   "requests": 0, "elapsed_sum": 0.0, "elapsed_n": 0}
               for k in day_keys}
    per_model = {}
    totals = {"runs": 0, "tasks": 0, "ok": 0, "failed": 0,
              "tokens": 0, "prompt": 0, "completion": 0,
              "requests": 0, "elapsed_sum": 0.0, "elapsed_n": 0}
    incidents = {}
    skipped = 0
    meta_reads = 0
    meta_capped = False
    limit = DAILY_TOKENS_SOFT
    prices = load_prices()
    if prices is not None:
        limit = int(prices.get("limits", {}).get("daily_tokens_soft", limit))
    money_counted = bool(prices and prices.get("tariff", {}).get("money_counted"))
    by_price = {}
    if prices is not None:
        by_price = {m["id"]: m for m in prices.get("models", [])
                    if isinstance(m, dict)}

    def _add(bucket, s):
        bucket["runs"] += 1
        bucket["tasks"] += int(s.get("total", 0))
        bucket["ok"] += int(s.get("ok", 0))
        bucket["failed"] += int(s.get("failed", 0))
        bucket["tokens"] += int(s.get("total_tokens", 0) or 0)
        bucket["prompt"] += int(s.get("prompt_tokens", 0) or 0)
        bucket["completion"] += int(s.get("completion_tokens", 0) or 0)
        bucket["requests"] += int(s.get("requests", 0) or 0)
        el = s.get("elapsed_s")
        if isinstance(el, (int, float)):
            bucket["elapsed_sum"] += float(el)
            bucket["elapsed_n"] += 1

    if os.path.isdir(RUNS_DIR):
        for name in sorted(os.listdir(RUNS_DIR)):
            sp = os.path.join(RUNS_DIR, name, "summary.json")
            if not os.path.isfile(sp):
                continue
            try:
                with open(sp, encoding="utf-8") as f:
                    s = json.load(f)
            except (OSError, ValueError):
                skipped += 1
                continue
            d = _samara_date(s.get("finished_at"))
            if d is None or d.isoformat() not in per_day:
                if d is None:
                    skipped += 1
                continue
            key = d.isoformat()
            _add(per_day[key], s)
            _add(totals, s)
            for t in s.get("tasks", []) or []:
                model = t.get("model") or "—"
                m = per_model.setdefault(model, {"tasks": 0, "ok": 0,
                                                "tokens": 0, "prompt": 0,
                                                "completion": 0})
                m["tasks"] += 1
                m["ok"] += 1 if t.get("ok") else 0
                m["tokens"] += int(t.get("total_tokens", 0) or 0)
                m["prompt"] += int(t.get("prompt_tokens", 0) or 0)
                m["completion"] += int(t.get("completion_tokens", 0) or 0)
                if t.get("ok"):
                    continue
                # Инцидент: причина из meta.json attempts; 0 попыток — без файла.
                attempts = t.get("attempts", 0)
                if not attempts:
                    incidents["0 попыток (не запущена)"] = \
                        incidents.get("0 попыток (не запущена)", 0) + 1
                    continue
                if meta_reads >= MAX_META_READS_WEEKLY:
                    meta_capped = True
                    continue
                meta_path = os.path.join(
                    t.get("dir") or os.path.join(RUNS_DIR, name,
                                                 f"task-{t.get('idx')}"),
                    "meta.json")
                try:
                    with open(meta_path, encoding="utf-8") as f:
                        meta = json.load(f)
                    meta_reads += 1
                except (OSError, ValueError):
                    incidents["meta.json не читается"] = \
                        incidents.get("meta.json не читается", 0) + 1
                    continue
                errs = [a.get("error", "") for a in meta.get("attempts", [])
                        if isinstance(a, dict)]
                if not errs:
                    incidents["без ошибки"] = incidents.get("без ошибки", 0) + 1
                    continue
                # Последняя попытка с ошибкой — причина падения задачи.
                last = next((e for e in reversed(errs) if str(e).strip()), "")
                cls = _classify_error(last)
                incidents[cls] = incidents.get(cls, 0) + 1
    cost = 0.0
    if money_counted:
        for model, m in per_model.items():
            entry = by_price.get(model, {})
            cost += (m["prompt"] * (entry.get("input_per_1m") or 0)
                     + m["completion"] * (entry.get("output_per_1m") or 0)) / 1_000_000
    top_models = sorted(per_model.items(), key=lambda kv: -kv[1]["tokens"])[:3]
    daily_ratio = {k: round(v["tokens"] / limit, 3) if limit else 0.0
                   for k, v in per_day.items()}
    avg_latency = (totals["elapsed_sum"] / totals["elapsed_n"]
                   if totals["elapsed_n"] else None)
    iso = end.isocalendar()
    return {
        "end": end.isoformat(), "start": days[0].isoformat(),
        "iso_week": f"{iso[0]}-W{iso[1]:02d}",
        "days": day_keys, "per_day": per_day, "per_model": per_model,
        "totals": totals, "incidents": incidents, "skipped": skipped,
        "meta_reads": meta_reads, "meta_capped": meta_capped,
        "cost": round(cost, 4), "money_counted": money_counted,
        "limit": limit, "daily_ratio": daily_ratio,
        "max_ratio": max(daily_ratio.values()) if daily_ratio else 0.0,
        "top_models": [(m, v["tokens"], v["tasks"]) for m, v in top_models],
        "avg_latency_s": round(avg_latency, 1) if avg_latency is not None else None,
        "generated_at": _now_samara().isoformat(timespec="seconds"),
    }


def render_weekly(data):
    """Markdown еженедельного отчёта по данным collect_week()."""
    t = data["totals"]
    lines = [
        f"# Еженедельный отчёт роутера — {data['iso_week']} "
        f"({data['start']} … {data['end']})",
        "",
        f"Сгенерирован: {data['generated_at']} (Самара). Провайдер: только Lix, "
        f"безлимит 0x до 09.11.2026 — стоимость 0, считаются токены и запросы.",
        "",
        "## Итоги недели",
        "",
        f"- Прогонов: {t['runs']}, задач: {t['tasks']} "
        f"(ok {t['ok']}, failed {t['failed']})",
        f"- Токены: {t['tokens']} (prompt {t['prompt']}, completion {t['completion']}), "
        f"запросов: {t['requests']}",
        f"- Стоимость: {data['cost']} (money_counted={data['money_counted']})",
        f"- Средняя латентность прогона: "
        f"{data['avg_latency_s']} с" if data["avg_latency_s"] is not None
        else "- Средняя латентность прогона: нет данных",
        f"- Макс. дневной расход к бюджету {data['limit']}: {data['max_ratio']:.0%}",
        "",
        "## По дням",
        "",
        "| День | Прогоны | Задачи ok/failed | Токены | Запросы | Ср. латентность | Бюджет |",
        "|---|---|---|---|---|---|---|",
    ]
    for k in data["days"]:
        v = data["per_day"][k]
        lat = (f"{v['elapsed_sum'] / v['elapsed_n']:.1f} с"
               if v["elapsed_n"] else "—")
        lines.append(
            f"| {k} | {v['runs']} | {v['ok']}/{v['failed']} | {v['tokens']} | "
            f"{v['requests']} | {lat} | {data['daily_ratio'][k]:.0%} |")
    lines += ["", "## По моделям", "",
              "| Модель | Задач | ok | Токены | Доля |",
              "|---|---|---|---|---|"]
    for model, m in sorted(data["per_model"].items(), key=lambda kv: -kv[1]["tokens"]):
        share = (m["tokens"] / t["tokens"] * 100) if t["tokens"] else 0.0
        lines.append(f"| {model} | {m['tasks']} | {m['ok']} | {m['tokens']} | {share:.1f}% |")
    lines += ["", "## Топ-3 модели по токенам", ""]
    for model, tokens, tasks in data["top_models"]:
        lines.append(f"- {model}: {tokens} токенов, {tasks} задач")
    if not data["top_models"]:
        lines.append("- нет данных")
    lines += ["", "## Инциденты", ""]
    if data["incidents"]:
        for cls, n in sorted(data["incidents"].items(), key=lambda kv: -kv[1]):
            lines.append(f"- {cls}: {n}")
    else:
        lines.append("- нет (все задачи за неделю ok)")
    if data["meta_capped"]:
        lines.append(f"- Примечание: чтение meta.json ограничено капом "
                     f"{MAX_META_READS_WEEKLY} (прочитано {data['meta_reads']}) — "
                     f"счёт инцидентов может быть неполным.")
    if data["skipped"]:
        lines.append(f"- Пропущено summary без даты: {data['skipped']}")
    lines += ["",
              f"Файл: reports/weekly-{data['iso_week']}.md",
              ""]
    return "\n".join(lines) + "\n"


def weekly_report(end_date=None):
    """Собрать отчёт за неделю, записать reports/weekly-YYYY-Www.md.
    Возвращает (путь, данные)."""
    data = collect_week(end_date)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, f"weekly-{data['iso_week']}.md")
    orch.atomic_write(path, render_weekly(data))
    _log_event("router", "еженедельный отчёт", "ok",
               f"{data['iso_week']}: {data['totals']['tokens']} токенов, "
               f"{data['totals']['ok']}/{data['totals']['tasks']} задач ok")
    return path, data


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
    marker = sync_budget_marker(budget)
    if marker:
        print(f"  бюджет-фолбэк АКТИВЕН: {marker.get('status')} "
              f"({marker.get('ratio', 0):.0%}), роль {marker.get('role')} -> "
              f"{marker.get('model')}, маркер state/router-budget")
    else:
        print("  бюджет-фолбэк: не активен")
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


def cmd_weekly(args):
    path, data = weekly_report(args.date)
    t = data["totals"]
    print(f"weekly {data['iso_week']} ({data['start']} … {data['end']}): "
          f"{t['tokens']} токенов, {t['ok']}/{t['tasks']} задач ok, "
          f"{t['runs']} прогонов, стоимость {data['cost']}")
    print(f"топ-3: {', '.join(m for m, _, _ in data['top_models']) or '—'}; "
          f"инциденты: {sum(data['incidents'].values())}; "
          f"макс. бюджет дня: {data['max_ratio']:.0%}")
    print(f"файл: {path}")
    return 0


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
    wk = sub.add_parser("weekly")
    wk.add_argument("--date", default=None,
                    help="конец недели YYYY-MM-DD (по умолчанию сегодня, Самара)")
    wk.set_defaults(fn=cmd_weekly)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
