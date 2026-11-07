#!/usr/bin/env python3
"""Юнит-тесты RAM guard v2 (fenrir_term.py). Только stdlib, без модели.

Запуск: python3 test_ram_guard_v2.py
Проверяет: границы порогов 300/500, dry-run и реальную чистку на
фейковом дереве (удаляется только allowlist-мусор), защиту занятых
путей, ротацию логов, lock, неизменность ram_guard().
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fenrir_term as ft

PASSED = 0
FAILED = 0


def check(name, cond, extra=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name} {extra}")


def _touch(path, content=b"x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


OLD = time.time() - 8 * 86400  # 8 дней назад — «устаревший»
FRESH = time.time()

# --- 1. Границы порогов ---
for mb, want in ((1000, "OK"), (500, "OK"), (499, "WARN"), (300, "WARN"),
                 (299, "CRITICAL"), (0, "CRITICAL")):
    check(f"classify {mb} -> {want}", ft._classify_ram(mb) == want,
          f"got {ft._classify_ram(mb)}")

# --- 2. Живое чтение MemAvailable ---
live = ft._read_mem_available_mb()
check("read_mem_available int > 0", isinstance(live, int) and live > 0,
      f"got {live!r}")
mb, st = ft.ram_guard()
check("ram_guard согласуется с classify",
      isinstance(mb, int) and st == ft._classify_ram(mb) and mb == live,
      f"got {(mb, st)} vs live {live}")

# --- 3. Чистка на фейковом дереве ---
with tempfile.TemporaryDirectory() as base:
    term = os.path.join(base, "term")
    orch = os.path.join(base, "orch")
    tmpd = os.path.join(base, "tmp")
    for d in (term, orch, tmpd):
        os.makedirs(d)
    # мусор, который ДОЛЖЕН удалиться
    _touch(os.path.join(term, "__pycache__", "a.pyc"), b"bytecode")
    _touch(os.path.join(orch, "__pycache__", "b.pyc"), b"bytecode")
    _touch(os.path.join(term, "logs", "app.log.1"), b"old", OLD)
    _touch(os.path.join(tmpd, "lix-queue-old.jsonl"), b"{}", OLD)
    # то, что НЕЛЬЗЯ трогать
    _touch(os.path.join(term, "runs", "r1", "summary.json"), b"{}")
    _touch(os.path.join(term, "state", "usage.json"), b"{}")
    _touch(os.path.join(term, "PLAN-EXTENDED.md"), b"# plan")
    _touch(os.path.join(term, "code.py"), b"print(1)")
    _touch(os.path.join(term, "logs", "fresh.log.1"), b"new", FRESH)
    _touch(os.path.join(tmpd, "lix-queue-fresh.jsonl"), b"{}", FRESH)
    _touch(os.path.join(tmpd, "other.txt"), b"data", OLD)

    logged = []
    n, freed, skipped = ft._cleanup_safe_garbage(
        dry_run=True, term_dir=term, orch_dir=orch, tmp_dir=tmpd,
        log_fn=logged.append)
    check("dry-run считает 4 объекта", n == 4, f"got {n}")
    check("dry-run ничего не удалил",
          os.path.exists(os.path.join(term, "__pycache__", "a.pyc"))
          and os.path.exists(os.path.join(term, "logs", "app.log.1"))
          and os.path.exists(os.path.join(tmpd, "lix-queue-old.jsonl")))
    check("dry-run логирует [DRY-RUN]",
          any("[DRY-RUN]" in m for m in logged))

    n, freed, skipped = ft._cleanup_safe_garbage(
        dry_run=False, term_dir=term, orch_dir=orch, tmp_dir=tmpd,
        log_fn=lambda m: None)
    check("реальная чистка удалила 4 объекта", n == 4, f"got {n}")
    check("pycache удалён",
          not os.path.exists(os.path.join(term, "__pycache__"))
          and not os.path.exists(os.path.join(orch, "__pycache__")))
    check("старый .log.1 удалён",
          not os.path.exists(os.path.join(term, "logs", "app.log.1")))
    check("старая очередь удалена",
          not os.path.exists(os.path.join(tmpd, "lix-queue-old.jsonl")))
    check("runs/ не тронут",
          os.path.exists(os.path.join(term, "runs", "r1", "summary.json")))
    check("state/ не тронут",
          os.path.exists(os.path.join(term, "state", "usage.json")))
    check("PLAN-EXTENDED.md не тронут",
          os.path.exists(os.path.join(term, "PLAN-EXTENDED.md")))
    check("код не тронут", os.path.exists(os.path.join(term, "code.py")))
    check("свежий .log.1 не тронут",
          os.path.exists(os.path.join(term, "logs", "fresh.log.1")))
    check("свежая очередь не тронута",
          os.path.exists(os.path.join(tmpd, "lix-queue-fresh.jsonl")))
    check("чужой файл в /tmp не тронут",
          os.path.exists(os.path.join(tmpd, "other.txt")))

# --- 4. Защита занятого пути ---
with tempfile.TemporaryDirectory() as base:
    tmpd = os.path.join(base, "tmp")
    os.makedirs(tmpd)
    busy = os.path.join(tmpd, "lix-queue-busy.jsonl")
    _touch(busy, b"{}", OLD)
    fd = os.open(busy, os.O_RDONLY)
    try:
        check("занятый файл определяется", ft._is_path_in_use(busy))
        logged = []
        n, _freed, skipped = ft._cleanup_safe_garbage(
            dry_run=False, term_dir=os.path.join(base, "t"),
            orch_dir=os.path.join(base, "o"), tmp_dir=tmpd,
            log_fn=logged.append)
        check("занятый файл пропущен чисткой",
              n == 0 and busy in skipped and os.path.exists(busy),
              f"got n={n} skipped={skipped}")
    finally:
        os.close(fd)
    check("свободный файл не считается занятым",
          not ft._is_path_in_use(busy))

# --- 5. Ротация логов ---
with tempfile.TemporaryDirectory() as base:
    logs = os.path.join(base, "logs")
    os.makedirs(logs)
    big = os.path.join(logs, "big.log")
    _touch(big, b"x" * 5000)
    logged = []
    rotated = ft._rotate_logs(logs, max_bytes=1000, log_fn=logged.append)
    check("большой лог ротирован", rotated == 1
          and os.path.exists(big + ".1")
          and not os.path.exists(big), f"got {rotated}")
    _touch(big, b"y" * 5000)  # новый большой лог, а .1 ещё свежий
    rotated = ft._rotate_logs(logs, max_bytes=1000, log_fn=lambda m: None)
    check("ротация пропущена при свежем .1", rotated == 0
          and os.path.exists(big), f"got {rotated}")
    os.utime(big + ".1", (OLD, OLD))  # .1 устарел
    rotated = ft._rotate_logs(logs, max_bytes=1000, log_fn=lambda m: None)
    check("ротация прошла после старения .1", rotated == 1
          and os.path.exists(big + ".1")
          and not os.path.exists(big), f"got {rotated}")
    small = os.path.join(logs, "small.log")
    _touch(small, b"z" * 100)
    rotated = ft._rotate_logs(logs, max_bytes=1000, log_fn=lambda m: None)
    check("маленький лог не ротируется", rotated == 0
          and os.path.exists(small), f"got {rotated}")

# --- 6. Lock ---
with tempfile.TemporaryDirectory() as base:
    lock = os.path.join(base, "ram-guard.lock")
    fd1 = ft._acquire_guard_lock(lock)
    check("первый lock берётся", fd1 is not None)
    fd2 = ft._acquire_guard_lock(lock)
    check("второй lock не берётся", fd2 is None)
    ft._release_guard_lock(fd1, lock)
    check("lock освобождается", not os.path.exists(lock))
    _touch(lock, b"123")
    os.utime(lock, (OLD, OLD))  # битый старый lock
    fd3 = ft._acquire_guard_lock(lock)
    check("битый lock пересоздаётся", fd3 is not None)
    ft._release_guard_lock(fd3, lock)

# --- 7. ram_guard_check dry-run на живой системе ---
# RAM за время теста могла пересечь порог (585 -> 451 МБ в этом прогоне),
# поэтому ожидания — по фактическому статусу, а не по «OK».
res = ft.ram_guard_check(dry_run=True)
check("guard_check возвращает dict с mb/status",
      abs(res["mb"] - ft._read_mem_available_mb()) <= 5
      and res["status"] == ft._classify_ram(res["mb"])
      and res["dry_run"] is True, f"got {res}")
if res["status"] == "OK":
    check("guard_check на OK ничего не делает", res["actions"] == []
          and res["cleaned"] == 0, f"got {res}")
else:
    check("guard_check на WARN/CRIT логирует и считает чистку",
          "log" in res["actions"] and res["cleaned"] >= 0, f"got {res}")
    check("dry-run реально ничего не удалил",
          os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "__pycache__")))

print(f"\nИТОГО: {PASSED} пройдено, {FAILED} провалено")
sys.exit(0 if FAILED == 0 else 1)
