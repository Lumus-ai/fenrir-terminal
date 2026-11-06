#!/usr/bin/env python3
"""Fenrir-Orchestra — личная лёгкая оркестра Shadow/Фенрира.

Только Python stdlib. Никакого Bun/TS-сервера, никакого фонового агента
group-чата (совет оригинала мёртв: агент чата ни разу не запускался,
токены 0 — проверено 06.11.2026). Консилиум = 6 параллельных прямых
API-вызовов + синтез; каждый результат сразу пишется на диск.

Тариф Lix (чек Shadow 06.11 ~04:14): безлимит на весь период до
09.11.2026, 260 RPM, 2 000 000 TPM, до 6 параллельных соединений.
Никогда не больше 6 одновременных вызовов; выжимать = параллелизм
и очередь без пауз, а не одиночные вызовы.

Ключи НЕ хранятся здесь: каждый запрос подписывается суррогатом через
dynamic_credentials.add_surrogate_to_request (коннекторы custom.lix,
custom.klinch, custom.anymodel, custom.anymodel2). В коде, логах и JSON
нет ни одного ключа/токена.

ПРИОРИТЕТ: Lix — №1 во всех ролях до дедлайна ключа 09.11.2026
(понедельник). Klinch/Anymodel — только фолбэк, когда все живые
Lix-модели мертвы. Мёртвые Lix-модели (403) перепроверяются ротацией
в каждом probe — могут ожить, навсегда не хоронятся.

Лечение потери длинных финалов (ответ ровно 6144 токена -> пустой финал):
max_tokens по умолчанию 2000, каждый ответ пишется на диск СРАЗУ,
атомарно (tmp + os.replace), пустой ответ считается failure, а не success.

Использование:
    python3 fenrir_orchestra.py probe
    python3 fenrir_orchestra.py run --task "..." [--role code|review|text]
    python3 fenrir_orchestra.py council --task "..." [--workers 3]
    python3 fenrir_orchestra.py batch --file tasks.jsonl [--max-tokens 2000]
    python3 fenrir_orchestra.py serve [--port 4097] [--host 127.0.0.1]
    python3 fenrir_orchestra.py status
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
from dynamic_credentials import add_surrogate_to_request, read_json_response  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "providers.json")
STATE_DIR = os.path.join(BASE, "state")
RUNS_DIR = os.path.join(BASE, "runs")
LOGS_DIR = os.path.join(BASE, "logs")
COOLDOWN_PATH = os.path.join(STATE_DIR, "cooldown.json")
COOLDOWN_SECONDS = 30 * 60          # 30 минут
# Единый User-Agent для всех исходящих запросов движка.
# Cloudflare 1010 блокирует дефолтный Python-urllib (проверено 06.11.2026:
# без UA Klinch отдавал 403 error code 1010 даже при живом ключе).
USER_AGENT = "fenrir-agent/1.0"
DEFAULT_MAX_TOKENS = 2000           # кап против потери длинных финалов
MAX_PARALLEL = 3                    # приказ Shadow 06.11.2026 ~19:22: НЕ БОЛЬШЕ 3 параллельных на Lix, всегда (его 3 сессии + мои 3 = лимит тарифа 6)
CHAT_TIMEOUT = 300
MODELS_TIMEOUT = 30
STARTED_AT = time.time()
_COOLDOWN_LOCK = threading.Lock()
_RUN_COUNTER = [0]


def _next_run_suffix():
    _RUN_COUNTER[0] += 1
    return f"{int(time.time() * 1000)}-{os.getpid()}-{_RUN_COUNTER[0]}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused")


# ---------------------------------------------------------------- utils
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def atomic_write(path, content):
    """Атомарная запись: tmp в той же папке + os.replace."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def atomic_write_json(path, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_cooldown():
    try:
        with open(COOLDOWN_PATH, encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        return {k: v for k, v in data.items() if v > now}
    except (OSError, ValueError):
        return {}


def save_cooldown(cd):
    atomic_write_json(COOLDOWN_PATH, cd)


def mark_cooldown(key):
    with _COOLDOWN_LOCK:
        cd = load_cooldown()
        cd[key] = time.time() + COOLDOWN_SECONDS
        save_cooldown(cd)


def is_cooldown(key):
    return key in load_cooldown()


def run_id():
    # Уникален даже при параллельных запусках в одну секунду (batch/council).
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + _next_run_suffix()


# ---------------------------------------------------------------- providers
def chat_once(provider, model, prompt, max_tokens):
    """Один chat-вызов. Возвращает (ok, content, meta, error).
    ok=False при HTTP-ошибке, пустом ответе или исключении."""
    cfg = provider
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": USER_AGENT},
        method="POST")
    add_surrogate_to_request(req, cfg["connector"], allowed_hosts=cfg["allowed_hosts"])
    t0 = time.time()
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=CHAT_TIMEOUT) as resp:
            data = read_json_response(resp)
    except urllib.error.HTTPError as e:
        return False, "", {"http_status": e.code}, f"HTTP {e.code}"
    except Exception as e:  # timeout, redirect refused, сеть
        return False, "", {}, f"{type(e).__name__}: {e}"
    elapsed = round(time.time() - t0, 1)
    try:
        choice = data["choices"][0]
        content = (choice["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return False, "", {"elapsed_s": elapsed}, "malformed response (no choices)"
    meta = {
        "provider": cfg["name"],
        "model_requested": model,
        "model_actual": data.get("model"),
        "finish_reason": choice.get("finish_reason"),
        "usage": data.get("usage"),
        "elapsed_s": elapsed,
    }
    if not content:
        return False, "", meta, "empty content (finish_reason=%s)" % choice.get("finish_reason")
    return True, content, meta, ""


def models_url(provider):
    """URL списка моделей. БАГ 06.11.2026: base_url уже оканчивается на
    /v1 (https://lix.autos/v1), поэтому base_url + '/v1/models' давал
    /v1/v1/models -> 404 и ложный models_ok=false при живом chat.
    Правильно: /v1/models = host-корень + /v1/models, т.е. base_url
    (уже .../v1) + '/models'."""
    base = provider["base_url"].rstrip("/")
    if base.endswith("/v1"):
        return base + "/models"
    return base + "/v1/models"


def list_models(provider):
    """GET /v1/models. Возвращает (ok, [ids], error)."""
    url = models_url(provider)
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT})
    add_surrogate_to_request(req, provider["connector"], allowed_hosts=provider["allowed_hosts"])
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=MODELS_TIMEOUT) as resp:
            data = read_json_response(resp)
    except urllib.error.HTTPError as e:
        return False, [], f"HTTP {e.code}"
    except Exception as e:
        return False, [], f"{type(e).__name__}: {e}"
    ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
    return True, ids, ""


def chain_for_role(cfg, role):
    """Фолбэк-цепочка (provider_name, model) для роли, без cooldown."""
    out = []
    for spec in cfg["roles"].get(role, cfg["roles"]["text"]):
        pname, model = spec.split("/", 1)
        if not is_cooldown(f"{pname}/{model}"):
            out.append((pname, model))
    return out


def lix_deadline_remaining(cfg):
    """Остаток времени до дедлайна ключа Lix. Возвращает строку вида '3д 4ч'."""
    raw = (cfg.get("lix") or {}).get("deadline")
    if not raw:
        return ""
    try:
        deadline = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    now = datetime.now(deadline.tzinfo) if deadline.tzinfo else datetime.now(timezone.utc)
    delta = deadline - now
    if delta.total_seconds() <= 0:
        return "ДЕДЛАЙН ПРОШЁЛ"
    days = delta.days
    hours = delta.seconds // 3600
    return f"{days}д {hours}ч до дедлайна 09.11.2026"


def pick_probe_model(cfg, pname, prov):
    """Модель для единственного короткого chat-вызова probe.
    Для Lix: ротация перепроверки мёртвых (403) моделей — одна за probe,
    не чаще раза в 30 мин на модель; если мёртвая не due — живая модель.
    Состояние ротации — state/lix_dead_recheck.json (только время, без ключей)."""
    if pname != "lix":
        for spec in cfg["roles"]["text"]:
            if spec.startswith(pname + "/"):
                return spec.split("/", 1)[1], False
        return None, False
    dead = (cfg.get("lix") or {}).get("dead_403_2026_11_06_0354", [])
    state_path = os.path.join(STATE_DIR, "lix_dead_recheck.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    now = time.time()
    due = [m for m in dead if now - state.get(m, 0) >= COOLDOWN_SECONDS]
    if due:
        model = due[0]
        state[model] = now
        atomic_write_json(state_path, state)
        return model, True
    for spec in cfg["roles"]["text"]:
        if spec.startswith("lix/"):
            return spec.split("/", 1)[1], False
    return None, False


# ---------------------------------------------------------------- probe
def cmd_probe(_args):
    cfg = load_config()
    results = {}
    any_alive = False
    for pname, prov in cfg["providers"].items():
        # Приказ Shadow 06.11.2026 ~11:30: только Lix — probe других
        # провайдеров не трогает вообще (даже тестовый «1+1=» тратит токены).
        if pname != "lix":
            continue
        ok, ids, err = list_models(prov)
        entry = {"models_ok": ok, "models_count": len(ids), "models": ids,
                 "probe": None, "error": err if not ok else ""}
        # /v1/models исправлен 06.11.2026 (models_url: base .../v1 + '/models'),
        # больше не даёт ложный 404 — chat-проверка и models-проверка обе живые.
        probe_model, is_recheck = pick_probe_model(cfg, pname, prov)
        if probe_model:
            ok2, content, meta, err2 = chat_once(prov, probe_model, "1+1=", 20)
            entry["probe"] = {"model": probe_model, "ok": ok2,
                              "recheck_dead": is_recheck,
                              "content": content[:50] if ok2 else "",
                              "finish_reason": meta.get("finish_reason"),
                              "error": err2}
            if ok2:
                any_alive = True
                if is_recheck:
                    # Мёртвая модель ожила — снять cooldown, чтобы run/council её брали.
                    cd = load_cooldown()
                    cd.pop(f"{pname}/{probe_model}", None)
                    save_cooldown(cd)
            else:
                mark_cooldown(f"{pname}/{probe_model}")
                if is_recheck:
                    # Перепроверка мёртвой модели упала — это НЕ значит, что
                    # мёртв весь провайдер. Один короткий вызов живой модели
                    # решает, жив ли провайдер на самом деле (исправление
                    # 06.11.2026: раньше probe писал 'lix dead HTTP 403',
                    # хотя живые модели отвечали).
                    live_model = None
                    for spec in cfg["roles"]["text"]:
                        if spec.startswith(pname + "/"):
                            live_model = spec.split("/", 1)[1]
                            break
                    if live_model:
                        ok3, content3, meta3, err3 = chat_once(
                            prov, live_model, "1+1=", 20)
                        entry["probe_live"] = {
                            "model": live_model, "ok": ok3,
                            "content": content3[:50] if ok3 else "",
                            "finish_reason": meta3.get("finish_reason"),
                            "error": err3}
                        if ok3:
                            any_alive = True
        else:
            entry["probe"] = {"ok": False, "error": "no model to probe"}
        results[pname] = entry
    remaining = lix_deadline_remaining(cfg)
    report = {"checked_at": now_iso(), "lix_deadline": (cfg.get("lix") or {}).get("deadline"),
              "lix_time_left": remaining, "providers": results}
    path = os.path.join(RUNS_DIR, f"health-{today()}.json")
    atomic_write_json(path, report)
    print(f"Lix: {remaining} (только Lix — приказ Shadow 06.11.2026)")
    print(f"{'provider':<12} {'models':<8} {'probe':<8} detail")
    for pname, e in results.items():
        probe = e["probe"] or {}
        live = e.get("probe_live") or {}
        # Статус провайдера — по живой модели, если перепроверка мёртвой упала.
        eff_ok = probe.get("ok") or live.get("ok")
        status = "alive" if eff_ok else "dead"
        tag = " [перепроверка мёртвой]" if probe.get("recheck_dead") else ""
        if live.get("ok"):
            detail = (live.get("content") or "-") + \
                f" (мёртвая {probe.get('model')} всё ещё {probe.get('error')})"
        else:
            detail = probe.get("content") or probe.get("error") or e.get("error") or "-"
        print(f"{pname:<12} {e['models_count']:<8} {status:<8} {detail}{tag}")
    print(f"report: {path}")
    return 0 if any_alive else 2


# ---------------------------------------------------------------- run
def do_run(task, role, max_tokens, out_dir=None, rid=None):
    """Пробегает фолбэк-цепочку роли. Пишет runs/<id>/answer.md + meta.json
    сразу при первом успехе. Возвращает (ok, rid, out_dir, meta_or_error)."""
    cfg = load_config()
    rid = rid or run_id()
    out_dir = out_dir or os.path.join(RUNS_DIR, rid)
    os.makedirs(out_dir, exist_ok=True)
    atomic_write_json(os.path.join(out_dir, "task.json"),
                      {"id": rid, "role": role, "task": task, "started_at": now_iso()})
    chain = chain_for_role(cfg, role)
    attempts = []
    for pname, model in chain:
        prov = cfg["providers"][pname]
        ok, content, meta, err = chat_once(prov, model, task, max_tokens)
        attempts.append({"provider": pname, "model": model, "ok": ok, "error": err,
                         "finish_reason": meta.get("finish_reason")})
        if ok:
            meta.update({"id": rid, "role": role, "task": task,
                         "attempts": attempts, "finished_at": now_iso()})
            atomic_write(os.path.join(out_dir, "answer.md"), content + "\n")
            atomic_write_json(os.path.join(out_dir, "meta.json"), meta)
            return True, rid, out_dir, meta
        # 403/429/502/пустой ответ -> следующий фолбэк; модель в cooldown
        mark_cooldown(f"{pname}/{model}")
    meta = {"id": rid, "role": role, "task": task,
            "attempts": attempts, "finished_at": now_iso(), "ok": False}
    atomic_write_json(os.path.join(out_dir, "meta.json"), meta)
    return False, rid, out_dir, meta


def cmd_run(args):
    ok, rid, out_dir, meta = do_run(args.task, args.role, args.max_tokens,
                                    out_dir=args.out)
    if ok:
        print(f"ok  id={rid} provider={meta['provider']} model={meta['model_actual']} "
              f"finish={meta.get('finish_reason')} usage={meta.get('usage')}")
        print(f"answer: {os.path.join(out_dir, 'answer.md')}")
        return 0
    print(f"FAIL id={rid}: все модели цепочки {args.role} мертвы/пусты")
    for a in meta["attempts"]:
        print(f"  {a['provider']}/{a['model']}: {a['error']}")
    return 1


# ---------------------------------------------------------------- council
COUNCIL_WORKERS = [
    ("coder", "code", "Ты coder. Реши задачу: напиши код или конкретный план. Коротко, по делу."),
    ("reviewer", "review", "Ты reviewer. Найди слабые места и риски в решении задачи, предложи исправления."),
    ("skeptic", "text", "Ты skeptic. Оспорь предположения задачи, найди что упущено и что может пойти не так."),
    ("architect", "review", "Ты architect. Предложи структуру решения: модули, интерфейсы, порядок шагов. Коротко, по делу."),
    ("tester", "code", "Ты tester. Придумай проверки для решения: тест-кейсы, краевые случаи, как убедиться что работает."),
    ("simplifier", "text", "Ты simplifier. Упрости решение: убери лишнее, найди самый короткий путь к тому же результату."),
]


def cmd_council(args):
    cfg = load_config()
    rid = run_id()
    out_dir = os.path.join(RUNS_DIR, rid)
    os.makedirs(out_dir, exist_ok=True)
    atomic_write_json(os.path.join(out_dir, "task.json"),
                      {"id": rid, "kind": "council", "task": args.task,
                       "workers": args.workers, "started_at": now_iso()})
    workers = COUNCIL_WORKERS[: max(1, min(args.workers, MAX_PARALLEL))]

    def worker_job(idx, name, role, system):
        # Каждый воркер — свой run в подпапке, со своей фолбэк-цепочкой.
        wdir = os.path.join(out_dir, f"worker-{idx + 1}-{name}")
        prompt = system + "\n\nЗадача:\n" + args.task
        ok, wrid, _, meta = do_run(prompt, role, DEFAULT_MAX_TOKENS, out_dir=wdir)
        # do_run пишет answer.md в wdir; для council кладём копию рядом как worker-N.md
        if ok:
            with open(os.path.join(wdir, "answer.md"), encoding="utf-8") as f:
                atomic_write(os.path.join(out_dir, f"worker-{idx + 1}.md"), f.read())
        return idx, name, ok, meta

    results = []
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        futs = {pool.submit(worker_job, i, n, r, s): n for i, (n, r, s) in enumerate(workers)}
        for fut in as_completed(futs):
            results.append(fut.result())
    results.sort()
    worker_texts = []
    for idx, name, ok, meta in results:
        p = os.path.join(out_dir, f"worker-{idx + 1}.md")
        if ok and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                worker_texts.append(f"## Воркер {idx + 1} ({name}, "
                                    f"{meta.get('provider')}/{meta.get('model_actual')})\n\n{f.read()}")
        else:
            worker_texts.append(f"## Воркер {idx + 1} ({name})\n\nFAILED: "
                                f"{(meta.get('attempts') or [{}])[-1].get('error', 'unknown')}")
    print(f"council {rid}: workers ok={sum(1 for r in results if r[2])}/{len(results)}")

    # Синтез — короткий вызов после воркеров; при падении — ручная сводка-заглушка, не пустота.
    synth_prompt = ("Сведи мнения воркеров ниже в один короткий итог: решение, "
                    "риски, что делать дальше. Максимум ~1500 токенов.\n\n" + "\n\n".join(worker_texts))
    ok, _srid, _d, meta = do_run(synth_prompt, "text", DEFAULT_MAX_TOKENS,
                                 out_dir=os.path.join(out_dir, "synthesis-tmp"))
    synth_path = os.path.join(out_dir, "synthesis.md")
    if ok:
        with open(os.path.join(out_dir, "synthesis-tmp", "answer.md"), encoding="utf-8") as f:
            atomic_write(synth_path, f.read())
        print(f"synthesis: {meta.get('provider')}/{meta.get('model_actual')}")
    else:
        stub = ("# Синтез (авто-синтез недоступен — все модели синтеза упали)\n\n"
                "Синтез-модель не ответила, поэтому итог — ручная сводка по файлам воркеров:\n\n")
        for idx, name, wok, _m in results:
            stub += f"- worker-{idx + 1}.md ({name}): {'ok' if wok else 'FAILED'}\n"
        stub += "\nЧитайте worker-файлы напрямую — они полные и сохранены на диске.\n"
        atomic_write(synth_path, stub)
        print("synthesis: FALLBACK stub (см. synthesis.md)")
    print(f"dir: {out_dir}")
    return 0 if any(r[2] for r in results) else 1


# ---------------------------------------------------------------- batch
def cmd_batch(args):
    """Очередь tasks.jsonl пулом ровно 3 воркеров без простоев.
    Каждая строка: {"task": "...", "role": "code|review|text"}.
    Как только воркер освободился — берёт следующую задачу. Каждый
    результат сразу на диск; в конце — runs/<batch>/summary.json
    с токенами и фактическими TPM/RPM. Никогда не больше 3
    одновременных вызовов (приказ Shadow 06.11.2026: его 3 сессии +
    мои 3 = лимит тарифа Lix 6)."""
    batch_id = run_id()
    out_dir = os.path.join(RUNS_DIR, batch_id)
    os.makedirs(out_dir, exist_ok=True)
    tasks = []
    invalid = 0
    with open(args.file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                task = item["task"]
                if not isinstance(task, str) or not task.strip():
                    raise ValueError("empty task")
            except (ValueError, KeyError, TypeError):
                invalid += 1
                continue
            role = item.get("role", "text")
            if role not in ("code", "review", "text"):
                role = "text"
            tasks.append({"task": task, "role": role})
    atomic_write_json(os.path.join(out_dir, "tasks.json"),
                      {"id": batch_id, "kind": "batch", "total": len(tasks),
                       "invalid_lines": invalid, "started_at": now_iso()})
    if not tasks:
        print(f"batch {batch_id}: нет валидных задач в {args.file} (invalid={invalid})")
        return 1
    t0 = time.time()
    workers = min(MAX_PARALLEL, len(tasks))

    def job(idx, item):
        tdir = os.path.join(out_dir, f"task-{idx + 1}")
        ok, rid, _, meta = do_run(item["task"], item["role"], args.max_tokens,
                                  out_dir=tdir, rid=f"{batch_id}-task-{idx + 1}")
        usage = meta.get("usage") or {}
        return {"idx": idx + 1, "role": item["role"], "ok": ok,
                "provider": meta.get("provider"), "model": meta.get("model_actual"),
                "prompt_tokens": usage.get("prompt_tokens", 0) or 0,
                "completion_tokens": usage.get("completion_tokens", 0) or 0,
                "total_tokens": usage.get("total_tokens", 0) or 0,
                "attempts": len(meta.get("attempts") or []),
                "dir": tdir}

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(job, i, item): i for i, item in enumerate(tasks)}
        for fut in as_completed(futs):
            results.append(fut.result())
            r = results[-1]
            print(f"  task-{r['idx']}: {'ok' if r['ok'] else 'FAIL'} "
                  f"{r['provider']}/{r['model']} tokens={r['total_tokens']}")
    results.sort(key=lambda r: r["idx"])
    elapsed = time.time() - t0
    minutes = elapsed / 60 if elapsed > 0 else 1e-9
    total_tokens = sum(r["total_tokens"] for r in results)
    summary = {
        "id": batch_id, "kind": "batch", "total": len(tasks),
        "ok": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "invalid_lines": invalid, "workers": workers,
        "max_parallel": MAX_PARALLEL,
        "prompt_tokens": sum(r["prompt_tokens"] for r in results),
        "completion_tokens": sum(r["completion_tokens"] for r in results),
        "total_tokens": total_tokens,
        "requests": sum(r["attempts"] for r in results),
        "elapsed_s": round(elapsed, 1),
        "tpm": round(total_tokens / minutes, 1),
        "rpm": round(sum(r["attempts"] for r in results) / minutes, 1),
        "finished_at": now_iso(), "tasks": results,
    }
    atomic_write_json(os.path.join(out_dir, "summary.json"), summary)
    print(f"batch {batch_id}: ok={summary['ok']}/{summary['total']} "
          f"tokens={total_tokens} elapsed={summary['elapsed_s']}s "
          f"TPM={summary['tpm']} RPM={summary['rpm']} (лимит 2M TPM / 260 RPM)")
    print(f"summary: {os.path.join(out_dir, 'summary.json')}")
    return 0 if summary["ok"] else 1


# ---------------------------------------------------------------- serve
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # не спамим stdout; ошибки — в stderr сервера

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            cfg = load_config()
            self._json(200, {"ok": True, "providers": list(cfg["providers"]),
                             "uptime_s": round(time.time() - STARTED_AT, 1)})
        elif self.path == "/runs":
            items = sorted(os.listdir(RUNS_DIR)) if os.path.isdir(RUNS_DIR) else []
            self._json(200, {"runs": items})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/run":
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
            task = payload["task"]
        except Exception as e:
            self._json(400, {"error": f"bad request: {e}"})
            return
        role = payload.get("role", "text")
        ok, rid, out_dir, meta = do_run(task, role, int(payload.get("max_tokens", DEFAULT_MAX_TOKENS)))
        self._json(200 if ok else 502,
                   {"ok": ok, "id": rid, "dir": out_dir,
                    "provider": meta.get("provider"), "model": meta.get("model_actual"),
                    "error": None if ok else str(meta.get("attempts"))})


def cmd_serve(args):
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fenrir-orchestra serve on http://{args.host}:{args.port} "
          f"(health: /health, run: POST /run, list: /runs)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    return 0


# ---------------------------------------------------------------- status
def cmd_status(_args):
    cfg = load_config()
    cd = load_cooldown()
    print(f"fenrir-orchestra  {now_iso()}  uptime-проверка локальная")
    print("providers (из providers.json, без ключей):")
    for pname, prov in cfg["providers"].items():
        print(f"  {pname:<12} {prov['base_url']}  connector={prov['connector']}")
    if cd:
        print(f"cooldown (мёртвые модели, 30 мин): {len(cd)}")
        for k, until in sorted(cd.items()):
            left = max(0, int(until - time.time()))
            print(f"  {k} — ещё {left // 60} мин {left % 60} с")
    else:
        print("cooldown: пуст (мёртвых моделей не зафиксировано)")
    runs = sorted(os.listdir(RUNS_DIR)) if os.path.isdir(RUNS_DIR) else []
    print(f"runs: {len(runs)}")
    for name in runs[-8:]:
        meta_p = os.path.join(RUNS_DIR, name, "meta.json")
        line = f"  {name}"
        if os.path.isfile(meta_p):
            try:
                with open(meta_p, encoding="utf-8") as f:
                    m = json.load(f)
                line += (f"  {m.get('provider', '?')}/{m.get('model_actual', '?')} "
                         f"finish={m.get('finish_reason')} ok={m.get('ok', True)}")
            except ValueError:
                pass
        print(line)
    return 0


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(prog="fenrir_orchestra")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    r = sub.add_parser("run")
    r.add_argument("--task", required=True)
    r.add_argument("--role", default="code", choices=["code", "review", "text"])
    r.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    r.add_argument("--out")
    c = sub.add_parser("council")
    c.add_argument("--task", required=True)
    c.add_argument("--workers", type=int, default=MAX_PARALLEL)
    b = sub.add_parser("batch")
    b.add_argument("--file", required=True)
    b.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=4097)
    s.add_argument("--host", default="127.0.0.1")
    sub.add_parser("status")
    args = p.parse_args()
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(RUNS_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    if args.cmd == "probe":
        return cmd_probe(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "council":
        return cmd_council(args)
    if args.cmd == "batch":
        return cmd_batch(args)
    if args.cmd == "serve":
        return cmd_serve(args)
    if args.cmd == "status":
        return cmd_status(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
