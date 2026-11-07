#!/usr/bin/env python3
"""Fenrir Terminal v3.1 — консилиум внутри терминала (обёртка).

Команда: fenrir council --task "..." [--workdir DIR] [--file F] [--dry-run]

Вызывает движок оркестры (fenrir_orchestra.py council31): три роли
(кодер/ревьюер/тестер) параллельно, максимум 3 вызова — приказ Shadow
06.11.2026 ~19:22; затем синтез координатора в synthesis.md и короткий
отчёт report.md. После прогона пишет одну строку в WORK_LOG.md.
Версия терминала не меняется — консилиум не версия, а команда.
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fenrir_worklog import worklog  # noqa: E402

ORCH = os.path.expanduser("~/workspace/fenrir-orchestra")
ENGINE = os.path.join(ORCH, "fenrir_orchestra.py")


def main(argv=None):
    p = argparse.ArgumentParser(prog="fenrir council")
    p.add_argument("--task")
    p.add_argument("--file")
    p.add_argument("--workdir")
    p.add_argument("--max-tokens", type=int, default=2000)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if not args.task and not args.file:
        print("fenrir council: нужна задача (--task или --file)")
        return 2
    cmd = [sys.executable, ENGINE, "council31"]
    if args.task:
        cmd += ["--task", args.task]
    if args.file:
        cmd += ["--file", args.file]
    if args.workdir:
        cmd += ["--workdir", args.workdir]
    cmd += ["--max-tokens", str(args.max_tokens)]
    if args.dry_run:
        cmd.append("--dry-run")
    rc = subprocess.call(cmd)
    label = (args.task or f"file:{args.file}")[:120]
    note = ("dry-run, модели не вызывались" if args.dry_run
            else ("консилиум ok" if rc == 0 else "консилиум FAILED"))
    if args.workdir:
        note += f", {args.workdir}"
    try:
        worklog("council", f"консилиум v3.1: {label}",
                "ok" if rc == 0 else "failed", note)
    except Exception:
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
