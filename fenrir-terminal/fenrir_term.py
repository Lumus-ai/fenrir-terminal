#!/usr/bin/env python3
"""Fenrir Terminal v1.2 — утилиты терминала (pure stdlib).

Источники:
- ram_guard() — ответ task-1 пачки прямых моделей Lix (qwen3.8-max),
  проверен живьём, взят как есть.
- lix_countdown() — ответ task-2 (qwen3.8-max), проверен живьём.
- health_summary() — task-4 ПЕРЕПИСАНА: исходный ответ содержал баг —
  итерировал health JSON как список, а это dict с ключом "providers",
  где каждая запись — dict с models_count и probe-dict (ok/error/model).
  Здесь структура учтена. v1.2: учитывает probe_live — если перепроверка
  мёртвой модели упала, а живая модель ответила, провайдер считается ok.
- doctor() / klinch_limits() / usage_today() — v1.2, написано напрямую.
"""
import json
import os
import shutil
import subprocess
import urllib.request
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    SAMARA = ZoneInfo("Europe/Samara")
except Exception:
    SAMARA = timezone(timedelta(hours=4))

LIX_DEADLINE = "2026-11-09T01:21:00+04:00"  # уточнение Shadow 06.11 14:21: осталось 2д 11ч
ORCH_DIR = os.environ.get("FENRIR_ORCHESTRA") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "fenrir-orchestra")
KLINCH_CONNECTORS = [
    ("klinch", "custom.klinch"),
    ("klinch2", "custom.klinch2"),
    ("klinch6", "custom.klinch6"),
    ("klinch7", "custom.klinch7"),
]


def ram_guard():
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) < 2:
                        break
                    mb_available = int(parts[1]) // 1024

                    if mb_available >= 500:
                        status = "OK"
                    elif mb_available >= 300:
                        status = "WARN"
                    else:
                        status = "CRITICAL"

                    return mb_available, status
    except (OSError, ValueError, IndexError):
        return 0, "CRITICAL"

    return 0, "CRITICAL"


def lix_countdown(deadline_str: str = LIX_DEADLINE) -> str:
    deadline = datetime.fromisoformat(deadline_str)

    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    delta = deadline - now

    if delta.total_seconds() <= 0:
        return "Lix истёк"

    total_hours = int(delta.total_seconds() // 3600)
    days, hours = divmod(total_hours, 24)

    if days:
        return f"до дедлайна Lix: {days}д {hours}ч"

    return f"до дедлайна Lix: {hours}ч"


def orch_dir():
    return os.path.abspath(ORCH_DIR)


def health_path():
    """Путь к сегодняшнему health-отчёту движка оркестры."""
    return os.path.join(orch_dir(), "runs", f"health-{date.today().isoformat()}.json")


def health_summary(path=None):
    """Одна строка на провайдера: имя, число моделей, probe ok/dead с ошибкой.

    health JSON — это dict: {"providers": {"lix": {"models_count": 22,
    "probe": {"ok": false, "error": "HTTP 403", ...}}, ...}}.
    Если probe — это упавшая перепроверка мёртвой модели, а probe_live
    ответил ok, провайдер считается ok (иначе probe врёт про Lix).
    """
    path = path or health_path()
    if not os.path.exists(path):
        return "health: нет данных probe"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return "health: нет данных probe"
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict) or not providers:
        return "health: нет данных probe"
    lines = []
    for name, entry in providers.items():
        count = entry.get("models_count", "?")
        probe = entry.get("probe") or {}
        live = entry.get("probe_live") or {}
        if probe.get("ok") or live.get("ok"):
            status = "ok"
            if live.get("ok") and not probe.get("ok"):
                status += f" (мёртвая {probe.get('model')} всё ещё {probe.get('error')})"
        else:
            err = probe.get("error") or entry.get("error") or ""
            status = f"dead: {err}" if err else "dead"
        lines.append(f"{name}, {count} моделей, probe {status}")
    return "\n".join(lines)


def _fmt_tokens(n):
    """40 096 229 -> '40,1 млн', 16 040 582 -> '16,0 млн'."""
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} млн".replace(".", ",")
    if n >= 1_000:
        return f"{n / 1_000:.0f} тыс"
    return str(n)


def doctor():
    """Самопроверка терминала. Возвращает (all_ok, [строки 'OK/FAIL ...'])."""
    checks = []
    # 1. serve отвечает?
    try:
        with urllib.request.urlopen("http://127.0.0.1:4097/health", timeout=3) as r:
            body = r.read(200).decode("utf-8", "replace")
        checks.append((True, f"serve: отвечает {body.strip()[:80]}"))
    except Exception as e:
        checks.append((False, f"serve: НЕ отвечает ({type(e).__name__})"))
    # 2. probe свежий (< 1 часа)?
    path = health_path()
    if os.path.exists(path):
        age_min = (datetime.now(timezone.utc).timestamp() - os.path.getmtime(path)) / 60
        if age_min < 60:
            checks.append((True, f"probe: свежий ({age_min:.0f} мин назад)"))
        else:
            checks.append((False, f"probe: устарел ({age_min:.0f} мин назад)"))
    else:
        checks.append((False, "probe: нет файла health за сегодня"))
    # 3. RAM
    mb, st = ram_guard()
    checks.append((st != "CRITICAL", f"RAM: {mb} МБ [{st}]"))
    # 4. Диск — файловая система рабочей папки (/home/hatch, 150 ГБ),
    # а не маленький overlay корня / (7,5 ГБ): он всегда покажет ~7 ГБ.
    try:
        free_gb = shutil.disk_usage(orch_dir()).free / (1024 ** 3)
        checks.append((free_gb >= 10, f"диск: свободно {free_gb:.0f} ГБ"))
    except OSError as e:
        checks.append((False, f"диск: не читается ({e})"))
    # 5. Lix до дедлайна?
    cd = lix_countdown()
    checks.append((not cd.startswith("Lix истёк"), f"Lix: {cd}"))
    lines = [f"{'OK  ' if ok else 'FAIL'}  {detail}" for ok, detail in checks]
    return all(ok for ok, _ in checks), lines


def klinch_limits():
    """Лимиты всех ключей Klinch одной таблицей + остаток дней Lix.

    Возвращает список строк. Каждый ключ опрашивается его же
    klinch_probe.py limits (ключ — через суррогат коннектора,
    значение нигде не читается и не выводится).
    """
    rows = []
    for short, connector in KLINCH_CONNECTORS:
        script = os.path.join(os.path.expanduser("~"), "workspace",
                              "custom-connectors", connector, "bin", "klinch_probe.py")
        try:
            out = subprocess.run(["python3", script, "limits"],
                                 capture_output=True, text=True, timeout=60)
            data = json.loads(out.stdout)
            lim = data["limits"]
            day = lim["day"]
            resets = day.get("resets_at") or ""
            try:
                dt = datetime.fromisoformat(resets.replace("Z", "+00:00"))
                resets_s = dt.astimezone(SAMARA).strftime("%d.%m %H:%M")
            except ValueError:
                resets_s = resets
            rows.append((short, _fmt_tokens(day.get("used")),
                         _fmt_tokens(day.get("left")), resets_s,
                         _fmt_tokens(lim.get("credited"))))
        except Exception as e:
            rows.append((short, "?", "?", f"ошибка: {type(e).__name__}", "?"))
    lines = ["ключ      использовано  осталось   сброс (Самара)  всего",
             "-" * 62]
    for short, used, left, resets, credited in rows:
        lines.append(f"{short:<9} {used:>11}  {left:>8}  {resets:<14}  {credited}")
    lines.append(f"Lix: {lix_countdown()} (безлимит до 09.11.2026)")
    return lines


def usage_today(day=None):
    """Токены за день по провайдерам из runs/*/meta.json.

    Считаются только meta.json (успешная попытка каждого вызова);
    summary.json не суммируется, чтобы не задвоить. Пишет
    state/usage-<дата>.json (дата — по Самаре) и возвращает строки таблицы.
    """
    day = day or datetime.now(SAMARA).date()
    runs_dir = os.path.join(orch_dir(), "runs")
    totals = {}
    files = 0
    for root, _dirs, names in os.walk(runs_dir):
        if "meta.json" not in names:
            continue
        try:
            with open(os.path.join(root, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        finished = meta.get("finished_at") or ""
        try:
            dt = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            if dt.astimezone(SAMARA).date() != day:
                continue
        except ValueError:
            continue
        usage = meta.get("usage") or {}
        total = usage.get("total_tokens")
        if total is None:
            continue
        prov = meta.get("provider") or "?"
        entry = totals.setdefault(prov, {"calls": 0, "prompt": 0,
                                         "completion": 0, "total": 0})
        entry["calls"] += 1
        entry["prompt"] += usage.get("prompt_tokens") or 0
        entry["completion"] += usage.get("completion_tokens") or 0
        entry["total"] += total
        files += 1
    state_dir = os.path.join(orch_dir(), "state")
    os.makedirs(state_dir, exist_ok=True)
    out_path = os.path.join(state_dir, f"usage-{day.isoformat()}.json")
    payload = {"date": day.isoformat(), "meta_files": files,
               "providers": totals}
    with open(out_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(out_path + ".tmp", out_path)
    lines = [f"токены за {day.strftime('%d.%m.%Y')} (по Самаре, {files} вызовов):",
             "провайдер    вызовов  промпт      ответ       всего",
             "-" * 55]
    grand = 0
    for prov, e in sorted(totals.items()):
        grand += e["total"]
        lines.append(f"{prov:<11} {e['calls']:>7}  {_fmt_tokens(e['prompt']):>9}  "
                     f"{_fmt_tokens(e['completion']):>9}  {_fmt_tokens(e['total']):>9}")
    lines.append(f"ИТОГО: {_fmt_tokens(grand)} токенов")
    lines.append(f"записано: {out_path}")
    return lines


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("ram", "all"):
        mb, st = ram_guard()
        print(f"RAM доступно: {mb} МБ [{st}]")
    if cmd in ("lix", "all"):
        print(lix_countdown())
    if cmd in ("health", "all"):
        print(health_summary())
    if cmd == "doctor":
        ok, lines = doctor()
        print("🐺 Фенрир Терминал — doctor")
        for line in lines:
            print(line)
        print("ИТОГ: OK" if ok else "ИТОГ: есть FAIL — смотри выше")
        raise SystemExit(0 if ok else 1)
    if cmd == "limits":
        for line in klinch_limits():
            print(line)
    if cmd == "usage":
        for line in usage_today():
            print(line)
