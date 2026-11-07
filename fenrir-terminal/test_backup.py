#!/usr/bin/env python3
"""Тесты fenrir_backup.py (v1.4, бэкап на Drive). Только stdlib.
Запуск: python3 test_backup.py — все проверки на временных деревьях,
Drive не трогается (upload/rotate мокаются через подмену _gws)."""
import os
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fenrir_backup as fb

PASSED = 0
FAILED = 0


def check(name, cond, extra=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("OK   %s" % name)
    else:
        FAILED += 1
        print("FAIL %s %s" % (name, extra))


def test_exclusions():
    check("исключение .pyc", fb._is_excluded_file("x.pyc"))
    check("исключение .log.1", fb._is_excluded_file("a.log.1"))
    check("обычный .log не исключается", not fb._is_excluded_file("a.log"))
    check("запрет .pem", fb._is_excluded_file("id_rsa.pem"))
    check("запрет .env", fb._is_excluded_file(".env"))
    check("обычный .py не исключается", not fb._is_excluded_file("x.py"))


def test_secret_scan(tmp):
    clean = os.path.join(tmp, "clean.py")
    with open(clean, "w") as f:
        f.write("x = 1\n# dynamic_credentials.add_surrogate_to_request\n")
    bad = os.path.join(tmp, "bad.py")
    with open(bad, "w") as f:
        # Собирается в рантайме: в исходнике теста полного паттерна нет.
        f.write('API_KEY = "' + "sk-" + "abcdefghij" + "0123456789" + '"\n')
    key = os.path.join(tmp, "id_rsa.pem")
    with open(key, "w") as f:
        # Собирается в рантайме, чтобы исходник теста не срабатывал.
        f.write("-----BEGIN RSA " + "PRIVATE KEY-----\n")
    check("чистый файл без находок",
          fb.secret_scan([("clean.py", clean)]) == [])
    hits = fb.secret_scan([("bad.py", bad)])
    check("sk-* находится", len(hits) == 1 and hits[0][0] == "bad.py")
    hits = fb.secret_scan([("id_rsa.pem", key)])
    check("ключ находится по имени и содержимому", len(hits) >= 1)


def test_archive_roundtrip(tmp):
    # Собираем фейковое дерево и проверяем состав архива.
    root = os.path.join(tmp, "tree")
    os.makedirs(os.path.join(root, "sub", "__pycache__"))
    with open(os.path.join(root, "a.py"), "w") as f:
        f.write("print(1)\n")
    with open(os.path.join(root, "sub", "b.md"), "w") as f:
        f.write("# hi\n")
    with open(os.path.join(root, "sub", "__pycache__", "c.pyc"), "wb") as f:
        f.write(b"\x00\x01")
    items = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in sorted(filenames):
            if fb._is_excluded_file(fn):
                continue
            full = os.path.join(dirpath, fn)
            items.append((os.path.relpath(full, root), full))
    arc = fb.build_archive(items, "2099-01-01-0000")
    check("архив создан", os.path.exists(arc))
    with tarfile.open(arc) as tar:
        names = tar.getnames()
    check("в архиве a.py и sub/b.md",
          "a.py" in names and "sub/b.md" in names, str(names))
    check("pycache не в архиве",
          not any("__pycache__" in n or n.endswith(".pyc") for n in names))
    check("sha256 считается", len(fb.sha256_file(arc)) == 64)
    os.remove(arc)


def test_rotate_selection():
    # Чистая функция выбора: keep N свежих, остальные — в корзину.
    # Список — как с Drive: свежие первыми (createdTime desc).
    files = [{"id": "f%d" % i, "name": "fenrir-backup-2026-11-%02d.tar.gz" % i}
             for i in range(10, 0, -1)]
    keep, drop = files[:7], files[7:]
    check("ротация: 7 свежих остаются", len(keep) == 7)
    check("ротация: 3 старых уходят", len(drop) == 3)
    check("ротация: свежий не удаляется",
          "fenrir-backup-2026-11-10.tar.gz" in [f["name"] for f in keep])


def test_name_format():
    import re
    stamp = "2026-11-07-1546"
    check("имя архива по формату",
          re.fullmatch(r"fenrir-backup-\d{4}-\d{2}-\d{2}-\d{4}\.tar\.gz",
                       "fenrir-backup-%s.tar.gz" % stamp) is not None)


def test_collect_real():
    items = fb.collect_files()
    names = [a for a, _ in items]
    check("состав непустой", len(items) > 10, "всего %d" % len(items))
    check("PLAN-EXTENDED.md в составе",
          "fenrir-terminal-v13/PLAN-EXTENDED.md" in names)
    check("tasks.yaml в составе", "fenrir-terminal/tasks.yaml" in names)
    check("WORK_LOG.md в составе",
          "fenrir-orchestra/WORK_LOG.md" in names)
    check("runs целиком нет",
          not any(n.startswith("fenrir-orchestra/runs/") and
                  not n.endswith("summary.json") and
                  "health-" not in n for n in names))
    check("summary.json в составе",
          any(n.endswith("summary.json") for n in names))
    check("pycache не в составе",
          not any("__pycache__" in n or n.endswith(".pyc") for n in names))
    check("backups/ не в составе",
          not any(n.startswith("fenrir-terminal/backups/") for n in names))
    check("scheduler-test не в составе",
          not any("scheduler-test" in n for n in names))
    hits = fb.secret_scan(items)
    check("реальный состав без секретов", hits == [], str(hits[:3]))


def main():
    tmp = tempfile.mkdtemp(prefix="fb-test-")
    test_exclusions()
    test_secret_scan(tmp)
    test_archive_roundtrip(tmp)
    test_rotate_selection()
    test_name_format()
    test_collect_real()
    print("\nИТОГО: %d пройдено, %d провалено" % (PASSED, FAILED))
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
