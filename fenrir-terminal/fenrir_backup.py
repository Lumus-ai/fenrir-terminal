#!/usr/bin/env python3
"""Fenrir Terminal v1.4 — бэкап на Google Drive (stdlib only).

Команда: `fenrir backup [--dry-run] [--no-upload] [--keep N]`

Что делает:
  1. Собирает tar.gz-архив ключевых активов терминала и оркестры
     (имя fenrir-backup-YYYY-MM-DD-HHMM.tar.gz, время Самары +04).
  2. Сканирует состав на секреты до загрузки — при находке прерывается.
  3. Загружает архив в папку «Fenrir Terminal Backups» на Google Drive
     (создаёт её при первом запуске) через коннектор google_drive
     (`hatch_gws_cli`, ключи/токены — только через коннектор).
  4. Ротация на Drive: хранятся последние 7 архивов, старые — в корзину.
  5. Проверка загрузки: скачивает архив обратно и сверяет sha256.
  6. Локальная ротация: хранятся последние 2 архива в backups/.

Состав архива (только активы, ничего лишнего):
  - fenrir-terminal/: код, tasks.yaml, digests/, CHANGELOG.md, логи.
  - fenrir-orchestra/: код, state/, WORK_LOG.md, логи.
    runs/ целиком НЕ входит (118 МБ): только summary.json последних
    20 прогонов и health-*.json — остальное уже влито в PLAN-EXTENDED.md.
  - fenrir-terminal-v13/PLAN-EXTENDED.md (40 МБ — главный актив).
Исключается: __pycache__, *.pyc, scheduler-test/, backups/ (сам архив),
временные деревья, acceptance-*, /tmp.

Секреты: ключи хранятся только в коннекторах (суррогаты), в файлах их
нет. Перед загрузкой состав сканируется на паттерны секретов
(sk-*, ghp_*, AKIA*, BEGIN PRIVATE KEY, password=...) и на запрещённые
имена (*.pem, *.key, .env*, *credentials*). state/cooldown.json содержит
только эпохи (числа) — допустим, но сканируется наравне со всеми.

Флаги:
  --dry-run   собрать архив и показать состав, без загрузки и ротации.
  --no-upload собрать архив и проверить локально (sha256), без загрузки.
  --keep N    хранить N последних архивов на Drive (по умолчанию 7).

Крон раз в день НЕ создаётся здесь — его ставит человек:
  0 3 * * * /home/hatch/workspace/fenrir-terminal/bin/fenrir backup
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone

TERM_DIR = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(TERM_DIR)
ORCH_DIR = os.path.join(WS, "fenrir-orchestra")
V13_DIR = os.path.join(WS, "fenrir-terminal-v13")
BACKUP_DIR = os.path.join(TERM_DIR, "backups")
LOG_FILE = os.path.join(TERM_DIR, "logs", "backup.log")
GWS = "/opt/hatch/bin/hatch_gws_cli"
FOLDER_NAME = "Fenrir Terminal Backups"
KEEP_ON_DRIVE = 7
KEEP_LOCAL = 2
SAMARA = timezone(timedelta(hours=4))
# Последние N прогонов оркестры: их summary.json входят в архив.
KEEP_RUN_SUMMARIES = 20

# Паттерны секретов: при совпадении бэкап прерывается до загрузки.
# Значения-плейсхолдеры из спек (ENV:, ${...}, YOUR_*, test_*, <...>)
# секретами НЕ считаются — см. _is_placeholder_value().
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|password|passwd|secret|token)"
               r"\s*[:=]\s*['\"]([^'\"]{8,})['\"]"),
    re.compile(r"hsurr-[A-Za-z0-9_-]{10,}"),
]
# Префиксы/признаки значений-плейсхолдеров в сгенерированных спеках.
PLACEHOLDER_PREFIXES = ("ENV:", "${", "<", "YOUR_", "your_", "test_",
                        "xxx", "XXX", "PLACEHOLDER", "placeholder",
                        "example", "123456:")
PLACEHOLDER_SUBSTRINGS = ("ABC-DEF", "XXXX", "****", "TODO", "CHANGEME",
                          "your_password", "test_token", "api_key_here")
# Имена файлов, которые в архив не попадают никогда.
FORBIDDEN_NAMES = (".pem", ".key", ".env", "credentials", ".gpg", ".p12", ".pfx")


def log(msg):
    line = "%s %s" % (datetime.now(SAMARA).strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1024 * 1024:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)


def _is_excluded_file(name):
    if name.endswith(".pyc") or name.endswith(".pyo"):
        return True
    low = name.lower()
    if low.endswith(".log.1"):
        return True
    for forb in FORBIDDEN_NAMES:
        if forb in low:
            return True
    return False


def collect_files():
    """Собирает список (arcname, abspath) для архива. Детерминированный порядок."""
    items = []

    def add_tree(root, arc_prefix, keep_fn=None):
        for dirpath, dirnames, filenames in os.walk(root):
            # Фильтруем каталоги по имени напрямую.
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in ("__pycache__", "backups", ".verify",
                                              "scheduler-test")
                                 and not d.startswith("acceptance-"))
            for fn in sorted(filenames):
                if _is_excluded_file(fn):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root)
                if keep_fn is not None and not keep_fn(rel):
                    continue
                items.append((os.path.join(arc_prefix, rel), full))

    add_tree(TERM_DIR, "fenrir-terminal")
    # runs/ оркестры — отдельно, только свежие summary.json + health-файлы.
    add_tree(ORCH_DIR, "fenrir-orchestra",
             keep_fn=lambda rel: not rel.startswith("runs" + os.sep))
    plan = os.path.join(V13_DIR, "PLAN-EXTENDED.md")
    if os.path.exists(plan):
        items.append(("fenrir-terminal-v13/PLAN-EXTENDED.md", plan))
    # runs: summary.json последних KEEP_RUN_SUMMARIES прогонов + health-*.json.
    runs_dir = os.path.join(ORCH_DIR, "runs")
    summaries = []
    if os.path.isdir(runs_dir):
        for name in sorted(os.listdir(runs_dir)):
            d = os.path.join(runs_dir, name)
            if not os.path.isdir(d):
                continue
            s = os.path.join(d, "summary.json")
            if os.path.exists(s):
                summaries.append((os.path.getmtime(s), s, name))
        summaries.sort(key=lambda t: t[0])
        for _, s, name in summaries[-KEEP_RUN_SUMMARIES:]:
            items.append((os.path.join("fenrir-orchestra", "runs", name,
                                       "summary.json"), s))
        for name in sorted(os.listdir(runs_dir)):
            if name.startswith("health-") and name.endswith(".json"):
                full = os.path.join(runs_dir, name)
                if os.path.isfile(full):
                    items.append((os.path.join("fenrir-orchestra", "runs", name),
                                  full))
    items.sort(key=lambda t: t[0])
    return items


def _is_placeholder_value(value):
    """Плейсхолдер из сгенерированных спек (не секрет)."""
    if value.startswith(PLACEHOLDER_PREFIXES):
        return True
    low = value.lower()
    return any(s.lower() in low for s in PLACEHOLDER_SUBSTRINGS)


def secret_scan(items):
    """Сканирует файлы состава на секреты. Возвращает список (arcname, что)."""
    hits = []
    for arcname, path in items:
        low = os.path.basename(path).lower()
        for forb in FORBIDDEN_NAMES:
            if forb in low:
                hits.append((arcname, "запрещённое имя файла: %s" % forb))
        try:
            with open(path, "rb") as f:
                data = f.read(2 * 1024 * 1024)
        except OSError as e:
            hits.append((arcname, "не читается: %s" % e))
            continue
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:
            continue
        found = None
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(text):
                # Паттерн key=value: проверяем значение на плейсхолдер.
                value = m.group(1) if m.lastindex else m.group(0)
                if m.lastindex and _is_placeholder_value(value):
                    continue
                found = m.group(0)
                break
            if found:
                break
        if found:
            hits.append((arcname, "паттерн секрета: %s..." % found[:24]))
    return hits


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_archive(items, stamp):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = "fenrir-backup-%s.tar.gz" % stamp
    path = os.path.join(BACKUP_DIR, name)
    with tarfile.open(path, "w:gz", compresslevel=6) as tar:
        for arcname, full in items:
            tar.add(full, arcname=arcname, recursive=False)
    return path


def _gws(args, timeout=300):
    cmd = [GWS] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("gws %s -> exit %d: %s" %
                           (" ".join(args[:3]), r.returncode,
                            (r.stderr or r.stdout).strip()[:500]))
    out = r.stdout.strip()
    return json.loads(out) if out else {}


def drive_folder_id():
    q = ("name = '%s' and mimeType = 'application/vnd.google-apps.folder' "
         "and trashed = false" % FOLDER_NAME)
    res = _gws(["drive", "files", "list", "--params",
                json.dumps({"q": q, "pageSize": 5,
                            "fields": "files(id,name)"})])
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    res = _gws(["drive", "files", "create", "--params",
                json.dumps({"ignoreDefaultVisibility": True}),
                "--json", json.dumps({"name": FOLDER_NAME,
                                      "mimeType": "application/vnd.google-apps.folder",
                                      "parents": ["root"]})])
    return res["id"]


def drive_upload(path, folder_id):
    name = os.path.basename(path)
    res = _gws(["drive", "+upload", path, "--parent", folder_id,
                "--name", name], timeout=600)
    return res


def drive_list_backups(folder_id):
    q = ("'%s' in parents and name contains 'fenrir-backup-' "
         "and trashed = false" % folder_id)
    res = _gws(["drive", "files", "list", "--params",
                json.dumps({"q": q, "pageSize": 50,
                            "orderBy": "createdTime desc",
                            "fields": "files(id,name,createdTime,size,webViewLink)"})])
    return res.get("files", [])


def drive_trash(file_id):
    _gws(["drive", "files", "update", "--params",
          json.dumps({"fileId": file_id}),
          "--json", json.dumps({"trashed": True})])


def drive_download(file_id, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    _gws(["drive", "files", "get", "--params",
          json.dumps({"fileId": file_id, "alt": "media"}),
          "--output", out_path], timeout=600)
    return out_path


def rotate_drive(folder_id, keep):
    files = drive_list_backups(folder_id)
    keep_files = files[:keep]
    drop_files = files[keep:]
    for f in drop_files:
        drive_trash(f["id"])
        log("ротация: в корзину %s (%s)" % (f["name"], f["id"]))
    return keep_files, drop_files


def prune_local(keep):
    names = sorted(n for n in os.listdir(BACKUP_DIR)
                   if n.startswith("fenrir-backup-") and n.endswith(".tar.gz"))
    for n in names[:-keep] if len(names) > keep else []:
        os.remove(os.path.join(BACKUP_DIR, n))
        log("локальная ротация: удалён %s" % n)
    return names[-keep:] if names else []


def cmd_backup(args):
    import argparse
    p = argparse.ArgumentParser(prog="fenrir backup")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--keep", type=int, default=KEEP_ON_DRIVE)
    ns = p.parse_args(args)
    stamp = datetime.now(SAMARA).strftime("%Y-%m-%d-%H%M")
    log("backup: старт (stamp %s)" % stamp)

    items = collect_files()
    log("backup: в состав %d файлов" % len(items))
    hits = secret_scan(items)
    if hits:
        for arcname, what in hits:
            log("СЕКРЕТ: %s — %s" % (arcname, what))
        print("ОШИБКА: в составе найдены секреты — загрузка прервана.")
        return 1
    log("backup: секретов не найдено")

    path = build_archive(items, stamp)
    size = os.path.getsize(path)
    digest = sha256_file(path)
    log("backup: архив %s, %d байт, sha256 %s" % (path, size, digest[:16]))
    if ns.dry_run:
        for arcname, _ in items:
            print("  " + arcname)
        print("dry-run: архив собран, загрузка пропущена.")
        return 0

    file_id = None
    link = None
    if not ns.no_upload:
        folder_id = drive_folder_id()
        log("backup: папка Drive %s" % folder_id)
        res = drive_upload(path, folder_id)
        file_id = res.get("id")
        link = res.get("webViewLink")
        log("backup: загружено на Drive, id %s" % file_id)
        # Проверка: скачать обратно и сверить sha256.
        verify_path = os.path.join(BACKUP_DIR, ".verify",
                                   os.path.basename(path))
        drive_download(file_id, verify_path)
        back_digest = sha256_file(verify_path)
        os.remove(verify_path)
        if back_digest != digest:
            log("ОШИБКА: sha256 скачанного архива не совпал!")
            print("ОШИБКА: проверка загрузки не прошла (sha256 не совпал).")
            return 1
        log("backup: проверка загрузки OK (sha256 совпал)")
        kept, dropped = rotate_drive(folder_id, ns.keep)
        log("backup: на Drive осталось %d архивов, в корзину %d"
            % (len(kept), len(dropped)))
    prune_local(KEEP_LOCAL)

    print("Архив: %s" % path)
    print("Размер: %d байт" % size)
    print("SHA256: %s" % digest)
    if file_id:
        print("Drive id: %s" % file_id)
        if link:
            print("Ссылка: %s" % link)
    return 0


def main():
    try:
        return cmd_backup(sys.argv[1:])
    except RuntimeError as e:
        log("ОШИБКА: %s" % e)
        print("ОШИБКА: %s" % e)
        return 1
    except KeyboardInterrupt:
        print("прервано")
        return 130


if __name__ == "__main__":
    sys.exit(main())
