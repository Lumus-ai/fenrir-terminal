#!/usr/bin/env python3
"""Fenrir Terminal — обновление из GitHub-репозитория (fenrir update).

    fenrir update [--check] [--dry-run]
    fenrir update --help

Репозиторий: https://github.com/Lumus-ai/fenrir-terminal
Дефолтная ветка: main (проверено живьём: git ls-remote --symref,
ref: refs/heads/main, HEAD 6af2f8e5).

Что делает:
- --check: только сравнивает локальный HEAD с удалённым (fetch обновляет
  только remote-tracking refs, файлы не трогаются) и говорит «уже свежий»
  или «есть обновления: N коммитов». Ничего не меняет.
- Без флагов: грязное дерево — отказ; fast-forward невозможен (локальные
  коммиты не на удалённом) — отказ; входящие изменения трогают защищённые
  файлы — отказ; иначе снапшот текущего кода (backups/, только код+конфиг),
  затем git pull --ff-only. После pull: py_compile изменённых .py + полный
  набор сьютов терминала; если проверки красные — код остаётся как есть,
  откат по снапшоту только по явной команде (сам не откатываю).
- Не git-копия (локального .git нет — как у Фенрира в песочнице, где пуш
  идёт через Git Data API): честно сообщает об этом, показывает удалённый
  HEAD и выходит без изменений.

Безопасность (жёстко):
- Никогда не трогает state/, logs/, tasks.yaml, WORK_LOG.md, очереди
  (content-queue.json, selfpatch-queue.json, outbox.json), content-renders/,
  digests/, reports/, backups/. Pull по определению трогает только
  отслеживаемые файлы репозитория; перед pull входящий diff проверяется
  на защищённые пути (см. PROTECTED_PATHS/PROTECTED_DIRS) — при совпадении
  обновление отклоняется.
- Проверка репозитория 07.11.2026 (git ls-files по свежему клону, 41 файл):
  НЕ отслеживаются: state/, logs/, WORK_LOG.md терминала, обе очереди,
  outbox.json, content-renders/, digests/, reports/, backups/.
  ИСКЛЮЧЕНИЯ — отслеживаются: fenrir-terminal/tasks.yaml и
  fenrir-orchestra/WORK_LOG.md. Именно поэтому перед pull проверяется
  входящий diff: если обновление трогает tasks.yaml или WORK_LOG.md —
  отказ, локальные файлы не меняются.
- Никаких eval/exec/os.system/shell=True; никаких секретов в коде и логах.
- Работа только внутри ~/workspace (папки терминала и оркестры).

Коды выхода: 0 — ок (включая «уже свежий», «есть обновления» в --check
и честный отказ для не-git-копии); 1 — отказ (грязное дерево, защищённые
файлы во входящих, pull не прошёл, проверки красные, неизвестный флаг,
сеть недоступна).
"""
import os
import subprocess
import sys
import tarfile
import time

REPO_URL = "https://github.com/Lumus-ai/fenrir-terminal.git"
DEFAULT_BRANCH = "main"
REMOTE_NAME = "origin"

# Защищённые пути (относительно корня репозитория). Pull их не трогает:
# перед pull входящий diff сверяется с этим списком.
PROTECTED_PATHS = {
    "fenrir-terminal/tasks.yaml",
    "fenrir-terminal/WORK_LOG.md",
    "fenrir-orchestra/WORK_LOG.md",
    "fenrir-terminal/content-queue.json",
    "fenrir-terminal/selfpatch-queue.json",
    "fenrir-terminal/outbox.json",
    "fenrir-terminal/projects.yaml",
}
PROTECTED_DIRS = (
    "fenrir-terminal/state/",
    "fenrir-terminal/logs/",
    "fenrir-terminal/content-renders/",
    "fenrir-terminal/digests/",
    "fenrir-terminal/reports/",
    "fenrir-terminal/backups/",
    "fenrir-orchestra/state/",
    "fenrir-orchestra/logs/",
    "fenrir-orchestra/runs/",
    "fenrir-orchestra/reports/",
)

# То же для снапшота, но относительно папки терминала/оркестры.
SNAPSHOT_SKIP_DIRS = {
    "state", "logs", "runs", "content-renders", "digests", "reports",
    "backups", "__pycache__", ".git",
}
SNAPSHOT_SKIP_FILES = {
    "WORK_LOG.md", "tasks.yaml", "content-queue.json",
    "selfpatch-queue.json", "outbox.json",
}
SNAPSHOT_KEEP = 5
GIT_TIMEOUT = 120
TEST_TIMEOUT = 300

HELP = """fenrir update — обновление терминала из GitHub (Lumus-ai/fenrir-terminal, ветка main).

Использование:
  fenrir update              обновить (снапшот -> pull --ff-only -> проверки)
  fenrir update --check      только проверить: свежий или есть обновления (N коммитов)
  fenrir update --dry-run    показать, что бы изменилось, ничего не меняя
  fenrir update --help       эта справка

Правила: грязное дерево — отказ; только fast-forward; входящие изменения
не должны трогать защищённые файлы (tasks.yaml, WORK_LOG.md, state/,
logs/, очереди, content-renders/, digests/, reports/). Не git-копия
(нет .git) — честный отказ с удалённым HEAD. После pull: py_compile
изменённых .py + все сьюты; при красных проверках код остаётся как есть,
откат по снапшоту — только по явной команде.

Коды выхода: 0 — ок; 1 — отказ/ошибка.
"""


def _short(sha):
    return sha[:7] if sha else "?"


def run_git(args, cwd, timeout=GIT_TIMEOUT):
    """git без shell. Возвращает (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True,
            timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", "git %s: %s" % (" ".join(args), exc)


def is_git_repo(path):
    rc, _, _ = run_git(["rev-parse", "--git-dir"], path)
    return rc == 0


def repo_root(term_dir):
    rc, out, _ = run_git(["rev-parse", "--show-toplevel"], term_dir)
    return out.strip() if rc == 0 and out.strip() else None


def local_head(repo):
    rc, out, _ = run_git(["rev-parse", "HEAD"], repo)
    return out.strip() if rc == 0 and out.strip() else None


def remote_head(repo, branch=DEFAULT_BRANCH):
    rc, out, _ = run_git(
        ["ls-remote", REMOTE_NAME, "refs/heads/%s" % branch], repo)
    if rc != 0 or not out.strip():
        return None
    return out.split()[0]


def remote_head_by_url(url=REPO_URL):
    """HEAD удалённого репозитория без локальной копии."""
    rc, out, _ = run_git(["ls-remote", url, "HEAD"], os.getcwd())
    if rc != 0 or not out.strip():
        return None
    return out.split()[0]


def fetch(repo):
    rc, _, err = run_git(["fetch", "--quiet", REMOTE_NAME], repo)
    return rc == 0, err.strip()


def count_commits(repo, rev_range):
    rc, out, _ = run_git(["rev-list", "--count", rev_range], repo)
    if rc != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def incoming_files(repo, branch=DEFAULT_BRANCH):
    """Файлы, которые изменит pull (HEAD..origin/branch)."""
    rc, out, _ = run_git(
        ["diff", "--name-only", "HEAD..%s/%s" % (REMOTE_NAME, branch)], repo)
    if rc != 0:
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def dirty_files(repo):
    """Локально изменённые ОТСЛЕЖИВАЕМЫЕ файлы (git status --porcelain).

    Неотслеживаемые (??) и игнорируемые (!!) не считаются грязью: само
    обновление создаёт backups/ и WORK_LOG.md, а py_compile — __pycache__;
    иначе первое же успешное обновление навсегда блокировало бы следующие.
    """
    rc, out, _ = run_git(["status", "--porcelain"], repo)
    if rc != 0:
        return None
    files = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        if line.startswith("??") or line.startswith("!!"):
            continue
        path = line[3:]
        if " -> " in path:  # rename: берём новое имя
            path = path.split(" -> ", 1)[1]
        files.append(path.strip())
    return files


def is_protected(rel_path):
    """Защищённый ли путь (относительно корня репозитория)."""
    if rel_path in PROTECTED_PATHS:
        return True
    return any(rel_path.startswith(d) for d in PROTECTED_DIRS)


def make_snapshot(term_dir, orch_dir):
    """Снапшот кода+конфига в backups/. Возвращает путь к tar.gz."""
    backups_dir = os.path.join(term_dir, "backups")
    os.makedirs(backups_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    tar_path = os.path.join(
        backups_dir, "fenrir-update-snapshot-%s.tar.gz" % ts)
    roots = [("fenrir-terminal", term_dir)]
    if orch_dir and os.path.isdir(orch_dir):
        roots.append(("fenrir-orchestra", orch_dir))
    with tarfile.open(tar_path, "w:gz") as tf:
        for prefix, root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d not in SNAPSHOT_SKIP_DIRS]
                for name in sorted(filenames):
                    if name in SNAPSHOT_SKIP_FILES:
                        continue
                    if name.endswith((".pyc", ".pyo")):
                        continue
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, root)
                    if any(part in SNAPSHOT_SKIP_DIRS
                           for part in rel.split(os.sep)):
                        continue
                    tf.add(full, arcname=os.path.join(prefix, rel))
    # Ротация: оставить SNAPSHOT_KEEP свежих.
    snaps = sorted(
        f for f in os.listdir(backups_dir)
        if f.startswith("fenrir-update-snapshot-") and f.endswith(".tar.gz"))
    for old in snaps[:-SNAPSHOT_KEEP]:
        try:
            os.remove(os.path.join(backups_dir, old))
        except OSError:
            pass
    return tar_path


def run_py_compile(py_files, repo):
    """Синтаксическая проверка изменённых .py (встроенный compile — без
    записи байткода, __pycache__ не создаётся). Возвращает список файлов
    с ошибкой."""
    bad = []
    for rel in py_files:
        full = os.path.join(repo, rel)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, encoding="utf-8") as f:
                compile(f.read(), full, "exec")
        except (SyntaxError, OSError, ValueError):
            bad.append(rel)
    return bad


def run_suites(term_dir, orch_dir):
    """Все сьюты терминала (test_*.py в обеих папках). Возвращает
    (ok, [(имя, ok, хвост)])."""
    results = []
    suites = []
    for base in (term_dir, orch_dir):
        if base and os.path.isdir(base):
            for name in sorted(os.listdir(base)):
                if name.startswith("test_") and name.endswith(".py"):
                    suites.append((base, name))
    for base, name in suites:
        full = os.path.join(base, name)
        try:
            proc = subprocess.run(
                [sys.executable, full], cwd=base,
                capture_output=True, text=True, timeout=TEST_TIMEOUT)
            tail = "\n".join(
                (proc.stdout + proc.stderr).splitlines()[-3:])
            results.append((name, proc.returncode == 0, tail))
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append((name, False, "запуск не удался: %s" % exc))
    ok = all(r[1] for r in results)
    return ok, results


def check_exec_bit(term_dir):
    """bin/fenrir исполняемый после pull?"""
    fenrir_bin = os.path.join(term_dir, "bin", "fenrir")
    if not os.path.isfile(fenrir_bin):
        return None
    return os.access(fenrir_bin, os.X_OK)


def resolve_term_dir(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("FENRIR_UPDATE_TERM_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.dirname(os.path.abspath(__file__))


def cmd_update(argv, term_dir=None):
    """Точка входа. argv — без 'update'. Возвращает код выхода."""
    term_dir = resolve_term_dir(term_dir)
    orch_dir = None
    args = list(argv)
    show_help = "--help" in args or "-h" in args
    check_only = "--check" in args
    dry_run = "--dry-run" in args
    known = {"--check", "--dry-run", "--help", "-h"}
    unknown = [a for a in args if a not in known]
    if unknown:
        print("fenrir update: неизвестный флаг: %s" % " ".join(unknown))
        print(HELP)
        return 1
    if show_help:
        print(HELP)
        return 0

    root = repo_root(term_dir)
    if root is None:
        # Не git-копия: честный отказ, показать удалённый HEAD.
        print("fenrir update: эта копия не обновляется через git "
              "(нет .git — развёрнута не через clone).")
        print("fenrir update: файлы не менялись.")
        head = remote_head_by_url()
        if head:
            print("fenrir update: удалённый HEAD (%s): %s"
                  % (DEFAULT_BRANCH, head))
            print("fenrir update: локальный HEAD неизвестен "
                  "(не git-копия).")
        else:
            print("fenrir update: не удалось узнать удалённый HEAD "
                  "(сеть недоступна?).")
        return 0

    orch_candidate = os.path.join(root, "fenrir-orchestra")
    if os.path.isdir(orch_candidate):
        orch_dir = orch_candidate

    ok_fetch, fetch_err = fetch(root)
    if not ok_fetch:
        print("fenrir update: не удалось связаться с удалённым "
              "репозиторием: %s" % fetch_err)
        return 1
    local = local_head(root)
    remote = remote_head(root)
    if not local or not remote:
        print("fenrir update: не удалось узнать HEAD "
              "(локальный=%s, удалённый=%s)"
              % (_short(local), _short(remote)))
        return 1
    behind = count_commits(root, "HEAD..%s/%s" % (REMOTE_NAME, DEFAULT_BRANCH))
    ahead = count_commits(root, "%s/%s..HEAD" % (REMOTE_NAME, DEFAULT_BRANCH))
    if behind is None or ahead is None:
        print("fenrir update: не удалось посчитать коммиты.")
        return 1

    dirty = dirty_files(root)
    if dirty is None:
        print("fenrir update: не удалось проверить состояние дерева.")
        return 1

    if check_only:
        print("fenrir update --check: локальный HEAD %s, удалённый %s."
              % (_short(local), _short(remote)))
        if behind == 0 and ahead == 0:
            print("fenrir update --check: уже свежий.")
        elif behind > 0:
            print("fenrir update --check: есть обновления: %d коммитов."
                  % behind)
        if ahead > 0:
            print("fenrir update --check: внимание: локально есть %d "
                  "коммитов, которых нет на удалённом — "
                  "pull --ff-only не пройдёт." % ahead)
        if dirty:
            print("fenrir update --check: грязное дерево (%d файлов) — "
                  "обновление будет отклонено до чистки." % len(dirty))
        print("fenrir update --check: файлы не менялись "
              "(fetch трогает только remote-tracking refs).")
        return 0

    if dirty:
        print("fenrir update: отказ — локально изменены файлы:")
        for path in dirty[:20]:
            print("  изменён: %s" % path)
        if len(dirty) > 20:
            print("  ... и ещё %d" % (len(dirty) - 20))
        print("fenrir update: уберите локальные правки (commit/stash/"
              "checkout), потом повторите. Ничего не менялось.")
        return 1
    if ahead > 0:
        print("fenrir update: отказ — локально есть %d коммитов, которых "
              "нет на удалённом; fast-forward невозможен." % ahead)
        print("fenrir update: ничего не менялось.")
        return 1
    if behind == 0:
        print("fenrir update: уже свежий (%s). Ничего не менялось."
              % _short(local))
        return 0

    incoming = incoming_files(root)
    if incoming is None:
        print("fenrir update: не удалось узнать входящие изменения.")
        return 1
    protected = [f for f in incoming if is_protected(f)]
    if protected:
        print("fenrir update: отказ — входящие изменения трогают "
              "защищённые файлы:")
        for path in protected:
            print("  защищён: %s" % path)
        print("fenrir update: локальные файлы не менялись.")
        return 1

    if dry_run:
        print("fenrir update --dry-run: было бы применено %d коммитов "
              "(%s -> %s)." % (behind, _short(local), _short(remote)))
        print("fenrir update --dry-run: входящие файлы (%d):"
              % len(incoming))
        for path in incoming[:20]:
            print("  %s" % path)
        if len(incoming) > 20:
            print("  ... и ещё %d" % (len(incoming) - 20))
        print("fenrir update --dry-run: был бы создан снапшот в "
              "backups/, затем pull --ff-only и проверки.")
        print("fenrir update --dry-run: ничего не изменено.")
        return 0

    snap = make_snapshot(term_dir, orch_dir)
    print("fenrir update: снапшот текущего кода: %s" % snap)
    rc, _, err = run_git(["pull", "--ff-only"], root)
    if rc != 0:
        last_err = err.strip().splitlines()[-1] if err.strip() else ""
        if last_err:
            print("fenrir update: pull --ff-only не прошёл: %s" % last_err)
        else:
            print("fenrir update: pull --ff-only не прошёл.")
        print("fenrir update: код оставлен как есть. Снапшот: %s" % snap)
        return 1
    new_head = local_head(root)
    print("fenrir update: pull ok: %s -> %s (%d коммитов)."
          % (_short(local), _short(new_head), behind))

    changed_py = [f for f in incoming if f.endswith(".py")]
    bad_py = run_py_compile(changed_py, root)
    suites_ok, suites = run_suites(term_dir, orch_dir)
    exec_bit = check_exec_bit(term_dir)
    problems = []
    if bad_py:
        problems.append("py_compile упал: %s" % ", ".join(bad_py))
    failed_suites = [name for name, ok, _ in suites if not ok]
    if failed_suites:
        problems.append("красные сьюты: %s" % ", ".join(failed_suites))
    if exec_bit is False:
        problems.append("bin/fenrir потерял бит исполнения")
    elif exec_bit is None:
        problems.append("bin/fenrir не найден после pull")
    print("fenrir update: проверки: py_compile %s, сьюты %d/%d зелёных."
          % ("ok" if not bad_py else "FAIL",
             sum(1 for _, ok, _ in suites if ok), len(suites)))
    for name, ok, tail in suites:
        if not ok:
            print("fenrir update: сьют %s FAIL: %s" % (name, tail))
    if problems:
        print("fenrir update: проверки НЕ прошли:")
        for problem in problems:
            print("  - %s" % problem)
        print("fenrir update: код оставлен как есть (pull уже применён, "
              "откат — только по явной команде). Снапшот: %s" % snap)
        _log_worklog(term_dir, "Обновление терминала",
                     "failed", "%s -> %s, проверки не прошли: %s"
                     % (_short(local), _short(new_head),
                        "; ".join(problems)))
        return 1

    print("fenrir update: готово, терминал обновлён до %s."
          % _short(new_head))
    _log_worklog(term_dir, "Обновление терминала", "ok",
                 "%s -> %s, %d коммитов, проверки зелёные"
                 % (_short(local), _short(new_head), behind))
    return 0


def _log_worklog(term_dir, task, status, note):
    """Строка в WORK_LOG.md терминала. Никогда не роняет обновление."""
    try:
        sys.path.insert(0, term_dir)
        import fenrir_worklog
        fenrir_worklog.worklog(
            "update", task, status, note,
            path=os.path.join(term_dir, "WORK_LOG.md"))
    except Exception:
        pass


def main():
    try:
        return cmd_update(sys.argv[1:])
    except KeyboardInterrupt:
        print("прервано")
        return 1


if __name__ == "__main__":
    sys.exit(main())
