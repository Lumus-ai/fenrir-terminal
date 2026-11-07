#!/usr/bin/env python3
"""Тесты обновления терминала (fenrir_update.py).

Запуск: python3 test_update.py
Все тесты работают только во временных папках с настоящим git и
фейковым remote (локальный bare-репозиторий). Живой терминал,
живой WORK_LOG.md и настоящий репозиторий не трогаются.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fenrir_update  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, cond, extra=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  ok: %s" % name)
    else:
        FAILED += 1
        print("  FAIL: %s %s" % (name, extra))


def git(*args, cwd):
    proc = subprocess.run(
        ["git"] + list(args), cwd=cwd, capture_output=True, text=True,
        timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("git %s in %s: %s"
                           % (" ".join(args), cwd, proc.stderr))
    return proc.stdout


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def make_remote(base):
    """Фейковый remote: origin_src -> bare remote.git. Возвращает путь."""
    src = os.path.join(base, "origin_src")
    os.makedirs(os.path.join(src, "fenrir-terminal", "bin"))
    os.makedirs(os.path.join(src, "fenrir-orchestra"))
    git("init", "-q", "-b", "main", cwd=src)
    git("config", "user.email", "test@example.local", cwd=src)
    git("config", "user.name", "Test", cwd=src)
    git("config", "commit.gpgsign", "false", cwd=src)
    write(os.path.join(src, "README.md"), "# test repo\n")
    write(os.path.join(src, "fenrir-terminal", "app.py"),
          "VALUE = 1\n")
    write(os.path.join(src, "fenrir-terminal", "bin", "fenrir"),
          "#!/bin/bash\necho fenrir\n", mode=0o755)
    write(os.path.join(src, "fenrir-terminal", "test_ok.py"),
          "print('ok')\n")
    # WORK_LOG.md терминала в репозитории НЕ отслеживается (как в
    # настоящем репозитории) — создаётся в рабочей копии, а не в коммите.
    write(os.path.join(src, "fenrir-terminal", "tasks.yaml"),
          "tasks: []\n")
    write(os.path.join(src, "fenrir-terminal", "content-queue.json"),
          "{}\n")
    write(os.path.join(src, "fenrir-orchestra", "engine.py"),
          "ENGINE = 1\n")
    git("add", "-A", cwd=src)
    git("commit", "-q", "-m", "initial", cwd=src)
    remote = os.path.join(base, "remote.git")
    git("clone", "-q", "--bare", src, remote, cwd=base)
    return remote


def make_work(base, remote):
    """Локальная git-копия из remote. Возвращает (root, term_dir)."""
    root = os.path.join(base, "work")
    git("clone", "-q", remote, root, cwd=base)
    git("config", "user.email", "test@example.local", cwd=root)
    git("config", "user.name", "Test", cwd=root)
    # Неотслеживаемый журнал, как в живой копии.
    write(os.path.join(root, "fenrir-terminal", "WORK_LOG.md"), "")
    return root, os.path.join(root, "fenrir-terminal")


def commit_to_remote(base, remote, files):
    """Новый коммит в remote через origin_src + push."""
    src = os.path.join(base, "origin_src")
    for rel, text in files.items():
        write(os.path.join(src, rel), text)
    git("add", "-A", cwd=src)
    git("commit", "-q", "-m", "update", cwd=src)
    git("push", "-q", remote, "HEAD:main", cwd=src)


def run_update(argv, term_dir):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fenrir_update.cmd_update(argv, term_dir=term_dir)
    return rc, buf.getvalue()


def head_of(root):
    return git("rev-parse", "HEAD", cwd=root).strip()


def test_check_fresh():
    print("test_check_fresh")
    with tempfile.TemporaryDirectory(prefix="upd-test-") as base:
        remote = make_remote(base)
        root, term = make_work(base, remote)
        before = head_of(root)
        app_before = read(os.path.join(term, "app.py"))
        rc, out = run_update(["--check"], term)
        check("rc 0", rc == 0, "rc=%s" % rc)
        check("уже свежий", "уже свежий" in out, out)
        check("HEAD не изменился", head_of(root) == before)
        check("файлы не менялись", read(os.path.join(term, "app.py")) == app_before)


def test_check_behind():
    print("test_check_behind")
    with tempfile.TemporaryDirectory(prefix="upd-test-") as base:
        remote = make_remote(base)
        root, term = make_work(base, remote)
        commit_to_remote(base, remote, {
            "fenrir-terminal/app.py": "VALUE = 2\n",
            "fenrir-terminal/new.py": "NEW = 1\n",
        })
        rc, out = run_update(["--check"], term)
        check("rc 0", rc == 0, "rc=%s" % rc)
        check("есть обновления: 1 коммитов",
              "есть обновления: 1 коммитов" in out, out)
        check("локально новый файл НЕ появился",
              not os.path.exists(os.path.join(term, "new.py")))
        check("локальный HEAD не изменился",
              "VALUE = 1" in read(os.path.join(term, "app.py")))


def test_update_applies_and_snapshot():
    print("test_update_applies_and_snapshot")
    with tempfile.TemporaryDirectory(prefix="upd-test-") as base:
        remote = make_remote(base)
        root, term = make_work(base, remote)
        commit_to_remote(base, remote, {
            "fenrir-terminal/app.py": "VALUE = 2\n",
            "fenrir-terminal/new.py": "NEW = 1\n",
        })
        rc, out = run_update([], term)
        check("rc 0", rc == 0, "rc=%s out=%s" % (rc, out))
        check("pull применил новый файл",
              os.path.exists(os.path.join(term, "new.py")))
        check("app.py обновлён",
              "VALUE = 2" in read(os.path.join(term, "app.py")))
        snaps = sorted(f for f in os.listdir(os.path.join(term, "backups"))
                       if f.startswith("fenrir-update-snapshot-"))
        check("снапшот создан", len(snaps) == 1, str(snaps))
        check("bin/fenrir исполняемый после pull",
              os.access(os.path.join(term, "bin", "fenrir"), os.X_OK))
        log = read(os.path.join(term, "WORK_LOG.md"))
        check("строка в WORK_LOG.md",
              "Обновление терминала" in log and "update" in log, log[-200:])
        # Содержимое снапшота: код есть, защищённого нет.
        with tarfile.open(os.path.join(term, "backups", snaps[0])) as tf:
            names = tf.getnames()
        check("в снапшоте есть код",
              any(n.endswith("fenrir-terminal/app.py") for n in names), str(names))
        check("в снапшоте нет tasks.yaml",
              not any(n.endswith("tasks.yaml") for n in names), str(names))
        check("в снапшоте нет WORK_LOG.md",
              not any(n.endswith("WORK_LOG.md") for n in names), str(names))
        check("в снапшоте нет content-queue.json",
              not any(n.endswith("content-queue.json") for n in names),
              str(names))
        # Второй запуск: уже свежий, без нового снапшота.
        rc2, out2 = run_update([], term)
        check("повтор: уже свежий", rc2 == 0 and "уже свежий" in out2, out2)
        snaps2 = [f for f in os.listdir(os.path.join(term, "backups"))
                  if f.startswith("fenrir-update-snapshot-")]
        check("повтор: снапшот не плодится", len(snaps2) == 1, str(snaps2))


def test_dirty_refused():
    print("test_dirty_refused")
    with tempfile.TemporaryDirectory(prefix="upd-test-") as base:
        remote = make_remote(base)
        root, term = make_work(base, remote)
        commit_to_remote(base, remote, {
            "fenrir-terminal/app.py": "VALUE = 2\n",
        })
        write(os.path.join(term, "app.py"), "VALUE = 99\n")  # грязное дерево
        before = head_of(root)
        rc, out = run_update([], term)
        check("rc 1", rc == 1, "rc=%s" % rc)
        check("сообщение называет файл",
              "app.py" in out and "отказ" in out, out)
        check("HEAD не изменился", head_of(root) == before)
        check("локальная правка на месте",
              "VALUE = 99" in read(os.path.join(term, "app.py")))
        backups = os.path.join(term, "backups")
        check("снапшот НЕ создан",
              not os.path.isdir(backups) or not os.listdir(backups))


def test_dry_run_changes_nothing():
    print("test_dry_run_changes_nothing")
    with tempfile.TemporaryDirectory(prefix="upd-test-") as base:
        remote = make_remote(base)
        root, term = make_work(base, remote)
        commit_to_remote(base, remote, {
            "fenrir-terminal/app.py": "VALUE = 2\n",
            "fenrir-terminal/new.py": "NEW = 1\n",
        })
        before = head_of(root)
        rc, out = run_update(["--dry-run"], term)
        check("rc 0", rc == 0, "rc=%s" % rc)
        check("сказано, что ничего не изменено",
              "ничего не изменено" in out, out)
        check("HEAD не изменился", head_of(root) == before)
        check("новый файл НЕ появился",
              not os.path.exists(os.path.join(term, "new.py")))
        backups = os.path.join(term, "backups")
        check("снапшот НЕ создан",
              not os.path.isdir(backups) or not os.listdir(backups))
        check("WORK_LOG.md не тронут",
              read(os.path.join(term, "WORK_LOG.md")) == "")


def test_non_git_copy():
    print("test_non_git_copy")
    with tempfile.TemporaryDirectory(prefix="upd-test-") as base:
        remote = make_remote(base)
        plain = os.path.join(base, "plain")
        shutil.copytree(os.path.join(base, "origin_src"), plain)
        shutil.rmtree(os.path.join(plain, ".git"))
        old_url = fenrir_update.REPO_URL
        fenrir_update.REPO_URL = remote  # ls-remote по фейковому remote
        try:
            rc, out = run_update([], plain + "/fenrir-terminal")
        finally:
            fenrir_update.REPO_URL = old_url
        check("rc 0 (информационный отказ)", rc == 0, "rc=%s" % rc)
        check("честно: не обновляется через git",
              "не обновляется через git" in out, out)
        check("удалённый HEAD показан",
              "удалённый HEAD" in out, out)
        check("файлы не менялись",
              "VALUE = 1" in read(os.path.join(plain, "fenrir-terminal",
                                                "app.py")))


def test_protected_incoming_refused():
    print("test_protected_incoming_refused")
    with tempfile.TemporaryDirectory(prefix="upd-test-") as base:
        remote = make_remote(base)
        root, term = make_work(base, remote)
        commit_to_remote(base, remote, {
            "fenrir-terminal/tasks.yaml": "tasks: [CHANGED]\n",
        })
        before = head_of(root)
        rc, out = run_update([], term)
        check("rc 1", rc == 1, "rc=%s" % rc)
        check("отказ называет tasks.yaml",
              "tasks.yaml" in out and "отказ" in out, out)
        check("локальный tasks.yaml не тронут",
              read(os.path.join(term, "tasks.yaml")) == "tasks: []\n")
        check("HEAD не изменился", head_of(root) == before)


def test_verify_failure_keeps_code():
    print("test_verify_failure_keeps_code")
    with tempfile.TemporaryDirectory(prefix="upd-test-") as base:
        remote = make_remote(base)
        root, term = make_work(base, remote)
        commit_to_remote(base, remote, {
            "fenrir-terminal/test_bad.py":
                "import sys\nprint('boom')\nsys.exit(1)\n",
        })
        rc, out = run_update([], term)
        check("rc 1", rc == 1, "rc=%s" % rc)
        check("pull всё же применён (файл на месте)",
              os.path.exists(os.path.join(term, "test_bad.py")))
        check("сообщение о красных сьютах",
              "test_bad.py" in out, out)
        snaps = [f for f in os.listdir(os.path.join(term, "backups"))
                 if f.startswith("fenrir-update-snapshot-")]
        check("снапшот создан", len(snaps) == 1, str(snaps))


def test_help_and_unknown_flag():
    print("test_help_and_unknown_flag")
    with tempfile.TemporaryDirectory(prefix="upd-test-") as base:
        remote = make_remote(base)
        _, term = make_work(base, remote)
        rc, out = run_update(["--help"], term)
        check("--help rc 0", rc == 0, "rc=%s" % rc)
        check("--help показывает справку",
              "fenrir update" in out and "--check" in out, out)
        rc2, out2 = run_update(["--bogus"], term)
        check("неизвестный флаг rc 1", rc2 == 1, "rc=%s" % rc2)
        check("неизвестный флаг назван",
              "--bogus" in out2, out2)


def main():
    tests = [
        test_check_fresh,
        test_check_behind,
        test_update_applies_and_snapshot,
        test_dirty_refused,
        test_dry_run_changes_nothing,
        test_non_git_copy,
        test_protected_incoming_refused,
        test_verify_failure_keeps_code,
        test_help_and_unknown_flag,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - честный FAIL, не падение
            global FAILED
            FAILED += 1
            print("  FAIL: %s рухнул: %s" % (test.__name__, exc))
    print("ИТОГ: %d ok, %d FAIL" % (PASSED, FAILED))
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
