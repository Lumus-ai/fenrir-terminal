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
- ram_guard v2 — пороги и эскалация, авточистка безопасного мусора
  (только __pycache__, старые /tmp-очереди Lix, старые ротированные
  логи), повторная проверка перед действием на CRIT, лог каждого
  срабатывания в logs/ram-guard.log. Суть взята из спеки пачек Lix
  1035/1041/1042 (шаги 2–3); psutil, /opt/fenrir, «Shadow Alert»,
  убийство процессов и канарейки из спеки НЕ брались — в реальном
  терминале их нет, serve/batch никогда не трогаются.
"""
import json
import os
import shutil
import subprocess
import time
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


# --- RAM guard v2: пороги (те же, что в v1) и эскалация ---
RAM_WARN_MB = 500   # ниже — WARN: лог + лёгкая чистка безопасного мусора
RAM_CRIT_MB = 300   # ниже — CRITICAL: повторная проверка + полная чистка
RAM_GUARD_LOG_MAX_BYTES = 1_048_576  # 1 МБ — порог ротации логов терминала
RAM_GUARD_STALE_DAYS = 7             # «устаревший» мусор старше 7 дней
RAM_GUARD_LOCK_STALE_S = 600         # lock старше 10 минут считается битым


def _classify_ram(mb):
    """Чистая функция порогов — для тестов на синтетических значениях."""
    if mb >= RAM_WARN_MB:
        return "OK"
    if mb >= RAM_CRIT_MB:
        return "WARN"
    return "CRITICAL"


def _read_mem_available_mb():
    """MemAvailable из /proc/meminfo в МБ; None, если не читается."""
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) < 2:
                        return None
                    return int(parts[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def ram_guard():
    mb = _read_mem_available_mb()
    if mb is None:
        return 0, "CRITICAL"
    return mb, _classify_ram(mb)


def _term_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _is_path_in_use(path):
    """True, если путь — cwd какого-то процесса или открыт им (fd).

    Проверка через /proc/*/cwd и /proc/*/fd (идея из шага 2 спеки):
    активный serve/batch, чей cwd или открытый файл лежит внутри пути,
    защищает его от чистки. Best-effort, ошибки чтения игнорируются.
    """
    target = os.path.realpath(path)
    try:
        pids = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
            if cwd == target or cwd.startswith(target + os.sep):
                return True
        except OSError:
            pass
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if link == target or link.startswith(target + os.sep):
                return True
    return False


def _dir_size(path):
    """Суммарный размер файлов под директорией (симлинки не считаются)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                continue
    return total


def _collect_cleanup_candidates(term_dir=None, orch_dir=None, tmp_dir="/tmp",
                               stale_days=RAM_GUARD_STALE_DAYS):
    """Allowlist кандидатов на удаление. Возвращает [(path, kind)].

    ТОЛЬКО явный мусор:
    - __pycache__ под терминалом и оркестрой (перегенерируемый байткод);
    - старые *.log.1 в logs/ терминала (ротированные логи старше N дней);
    - старые /tmp-файлы очередей Lix (lix-queue-*.jsonl старше N дней).
    НИКОГДА: runs/, state/, PLAN-EXTENDED.md, код, логи моложе N дней,
    свежие очереди, файлы пользователя. runs/ и state/ не обходятся вовсе.
    """
    term_dir = term_dir or _term_dir()
    orch_dir = orch_dir if orch_dir is not None else orch_dir()
    stale_after = time.time() - stale_days * 86400
    candidates = []
    forbidden = {"runs", "state"}
    for base in (term_dir, orch_dir):
        if not base or not os.path.isdir(base):
            continue
        for root, dirs, _files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in forbidden
                       and not os.path.islink(os.path.join(root, d))]
            if "__pycache__" in dirs:
                p = os.path.join(root, "__pycache__")
                if not os.path.islink(p):
                    candidates.append((p, "pycache"))
                dirs.remove("__pycache__")
    logs_dir = os.path.join(term_dir, "logs")
    if os.path.isdir(logs_dir):
        for name in os.listdir(logs_dir):
            if not name.endswith(".log.1"):
                continue
            p = os.path.join(logs_dir, name)
            try:
                if (os.path.isfile(p) and not os.path.islink(p)
                        and os.path.getmtime(p) < stale_after):
                    candidates.append((p, "old-rotated-log"))
            except OSError:
                continue
    if tmp_dir and os.path.isdir(tmp_dir):
        for name in os.listdir(tmp_dir):
            if not ((name.startswith("lix-queue-") and name.endswith(".jsonl"))
                    or name == "lix-check-alive.jsonl"):
                continue
            p = os.path.join(tmp_dir, name)
            try:
                if (os.path.isfile(p) and not os.path.islink(p)
                        and os.path.getmtime(p) < stale_after):
                    candidates.append((p, "old-tmp-queue"))
            except OSError:
                continue
    return candidates


def _cleanup_safe_garbage(dry_run=False, term_dir=None, orch_dir=None,
                          tmp_dir="/tmp", log_fn=None):
    """Удаляет только allowlist-мусор. Возвращает
    (удалено_объектов, освобождено_байт, пропущено_занятых).
    В dry-run ничего не удаляет, только логирует, что удалил бы.
    """
    log_fn = log_fn or (lambda m: None)
    deleted, freed, skipped = 0, 0, []
    for path, kind in _collect_cleanup_candidates(term_dir, orch_dir, tmp_dir):
        if _is_path_in_use(path):
            skipped.append(path)
            log_fn(f"пропущено (занято процессом): {path}")
            continue
        try:
            size = (_dir_size(path) if os.path.isdir(path)
                    else os.path.getsize(path))
        except OSError:
            size = 0
        if dry_run:
            log_fn(f"[DRY-RUN] удалил бы ({kind}): {path} ({size} байт)")
            deleted += 1
            freed += size
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as e:
            log_fn(f"ошибка удаления {path}: {e}")
            continue
        log_fn(f"удалено ({kind}): {path} ({size} байт)")
        deleted += 1
        freed += size
    return deleted, freed, skipped


def _rotate_logs(logs_dir, max_bytes=RAM_GUARD_LOG_MAX_BYTES,
                 stale_days=RAM_GUARD_STALE_DAYS, log_fn=None):
    """Ротация логов > max_bytes: текущий -> .1.

    Старый .1 старше stale_days удаляется первым; если .1 ещё свежий —
    текущий лог не трогается (не теряем свежие данные). Возвращает
    число ротированных файлов.
    """
    log_fn = log_fn or (lambda m: None)
    rotated = 0
    if not os.path.isdir(logs_dir):
        return 0
    for name in sorted(os.listdir(logs_dir)):
        if not name.endswith(".log"):
            continue
        p = os.path.join(logs_dir, name)
        try:
            if not os.path.isfile(p) or os.path.islink(p):
                continue
            if os.path.getsize(p) <= max_bytes:
                continue
        except OSError:
            continue
        if _is_path_in_use(p):
            log_fn(f"ротация пропущена (лог занят): {p}")
            continue
        old = p + ".1"
        if os.path.exists(old):
            try:
                if os.path.getmtime(old) < time.time() - stale_days * 86400:
                    os.remove(old)
                    log_fn(f"удалён старый ротированный лог: {old}")
                else:
                    log_fn(f"ротация пропущена (свежий .1): {p}")
                    continue
            except OSError as e:
                log_fn(f"ошибка ротации {p}: {e}")
                continue
        try:
            os.replace(p, old)
        except OSError as e:
            log_fn(f"ошибка ротации {p}: {e}")
            continue
        log_fn(f"ротирован лог: {p} -> {old}")
        rotated += 1
    return rotated


def _acquire_guard_lock(lock_path, stale_s=RAM_GUARD_LOCK_STALE_S):
    """Защита от параллельного запуска чистки. Возвращает fd или None."""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        except OSError:
            pass
        return fd
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(lock_path) > stale_s:
                os.remove(lock_path)
                return _acquire_guard_lock(lock_path, stale_s)
        except OSError:
            pass
        return None
    except OSError:
        return None


def _release_guard_lock(fd, lock_path):
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(lock_path)
    except OSError:
        pass


def ram_guard_check(dry_run=False, recheck_delay=5):
    """RAM guard v2: эскалация по порогам (те же 500/300, что в v1).

    OK — ничего не делает. WARN — лог + лёгкая чистка безопасного
    мусора. CRIT — повторная проверка через recheck_delay с: если не
    подтверждается, чистка отменяется (защита от ложных срабатываний);
    если подтверждается — лог (в т.ч. в ram-warnings.log, как в v1) +
    полная чистка, включая ротацию логов. Возвращает dict с итогом.
    Никогда не трогает serve/batch, runs/, state/, PLAN-EXTENDED.md,
    код терминала и файлы пользователя. Выход guard — всегда без
    исключений наружу (ошибки логируются, а не падают).
    """
    term_dir = _term_dir()
    logs_dir = os.path.join(term_dir, "logs")
    try:
        os.makedirs(logs_dir, exist_ok=True)
    except OSError:
        pass
    guard_log = os.path.join(logs_dir, "ram-guard.log")

    def log(msg):
        line = (f"{datetime.now(SAMARA).strftime('%Y-%m-%d %H:%M:%S')} "
                f"{msg}\n")
        try:
            with open(guard_log, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    mb = _read_mem_available_mb()
    result = {"mb": mb if mb is not None else 0, "status": "CRITICAL",
              "actions": [], "cleaned": 0, "freed_bytes": 0,
              "dry_run": dry_run, "log": guard_log}
    if mb is None:
        log("CRITICAL: не читается /proc/meminfo")
        result["actions"].append("log")
        return result
    status = _classify_ram(mb)
    result["status"] = status
    if status == "OK":
        return result
    lock_path = os.path.join(logs_dir, "ram-guard.lock")
    if status == "WARN":
        log(f"WARN: доступно {mb} МБ (< {RAM_WARN_MB}) — лёгкая чистка")
        result["actions"].append("log")
        fd = _acquire_guard_lock(lock_path)
        if fd is None:
            log("WARN: чистка пропущена — уже запущена (lock)")
            result["actions"].append("lock-skip")
            return result
        try:
            n, freed, skipped = _cleanup_safe_garbage(
                dry_run=dry_run, term_dir=term_dir, orch_dir=orch_dir(),
                log_fn=log)
            result.update(cleaned=n, freed_bytes=freed,
                          skipped_in_use=len(skipped))
            result["actions"].append("cleanup-light")
            log(f"WARN: чистка завершена: удалено {n}, "
                f"освобождено ~{freed} байт")
        finally:
            _release_guard_lock(fd, lock_path)
        return result
    # CRITICAL — повторная проверка перед любым действием.
    log(f"CRITICAL: доступно {mb} МБ (< {RAM_CRIT_MB}) — "
        f"повторная проверка через {recheck_delay} с")
    time.sleep(recheck_delay)
    mb2 = _read_mem_available_mb()
    if mb2 is None:
        mb2 = mb
    status2 = _classify_ram(mb2)
    result["mb_after"] = mb2
    result["status_after"] = status2
    if status2 != "CRITICAL":
        log(f"CRITICAL не подтвердился: {mb} -> {mb2} МБ [{status2}] — "
            f"чистка отменена (ложное срабатывание)")
        result["actions"].append("recheck-cancel")
        return result
    log(f"CRITICAL подтверждён: {mb2} МБ — полная чистка")
    result["actions"].append("log")
    try:
        with open(os.path.join(logs_dir, "ram-warnings.log"),
                  "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(SAMARA).strftime('%Y-%m-%d %H:%M:%S')} "
                    f"Critical RAM warning: available {mb2} MB "
                    f"(rechecked)\n")
    except OSError:
        pass
    fd = _acquire_guard_lock(lock_path)
    if fd is None:
        log("CRITICAL: чистка пропущена — уже запущена (lock)")
        result["actions"].append("lock-skip")
        return result
    try:
        n, freed, skipped = _cleanup_safe_garbage(
            dry_run=dry_run, term_dir=term_dir, orch_dir=orch_dir(),
            log_fn=log)
        rotated = _rotate_logs(logs_dir, log_fn=log)
        result.update(cleaned=n, freed_bytes=freed,
                      skipped_in_use=len(skipped), rotated=rotated)
        result["actions"].append("cleanup-full")
        log(f"CRITICAL: чистка завершена: удалено {n}, "
            f"освобождено ~{freed} байт, ротировано логов: {rotated}")
    finally:
        _release_guard_lock(fd, lock_path)
    return result


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
    if cmd == "guard":
        dry = "--dry-run" in sys.argv[2:]
        res = ram_guard_check(dry_run=dry)
        print(f"RAM guard v2: {res['mb']} МБ [{res['status']}]"
              + (" (dry-run)" if dry else ""))
        if res.get("mb_after") is not None:
            print(f"повторная проверка: {res['mb_after']} МБ "
                  f"[{res['status_after']}]")
        for action in res["actions"]:
            print(f"  действие: {action}")
        if res.get("cleaned"):
            verb = "удалило бы объектов" if dry else "удалено объектов"
            print(f"  {verb}: {res['cleaned']}, "
                  f"освобождено ~{res['freed_bytes']} байт")
        if res.get("rotated"):
            print(f"  ротировано логов: {res['rotated']}")
        print(f"  лог: {res['log']}")
    if cmd == "limits":
        for line in klinch_limits():
            print(line)
    if cmd == "usage":
        for line in usage_today():
            print(line)
