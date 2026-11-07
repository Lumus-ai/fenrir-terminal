#!/usr/bin/env python3
"""Fenrir Terminal v1.4 — авто-журлог (WORK_LOG.md).

Каждая выполненная задача сама пишет сюда строку. Формат — пересмотренный
v1.4 из спеки (PLAN-EXTENDED.md, «Формат автожурнала v1.4»):

    Дата | Время | Исполнитель | Задача | Статус | Примечания | Комментарий (авто)

Пример:
    2026-11-07 | 15:52 | scheduler | TST-002 Проверка статуса | ok |
    выполнено через custom.klinch/gpt-6-luna | [авто]

Отступления от спеки (сознательные, задокументированы):
- Статус — ok/failed, как везде в терминале (scheduler, agent), а не
  «Завершено»/«Ошибка» из примера спеки: единый словарь статусов.
- Ротация статусов по таймеру (12 ч) из спеки НЕ берётся: журлог —
  append-only история фактов, статусы задач живут в tasks.yaml.
- Исполнитель — компонент (scheduler/agent/guard/backup/digest), а не
  имя человека из примера спеки.

Правила:
- Только факты: запись делается в момент выполнения действия.
- Без дублей: строка, уже присутствующая в файле, не пишется повторно
  (повторный прогон того же действия в ту же минуту даёт ту же строку).
- Запись атомарна: append + flush + fsync; файл создаётся с заголовком
  при первом запуске, ретроспективных записей не делается.
- Ручные правки разрешены; авто-записи помечены [авто].
- Хелпер никогда не роняет вызывающий код: ошибки OSError глушатся.
- Путь журнала: <папка модуля>/WORK_LOG.md; переопределяется env
  FENRIR_WORKLOG (для тестов) или явным аргументом path.
"""
import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    SAMARA = ZoneInfo("Europe/Samara")
except Exception:  # pragma: no cover - нет tzdata
    SAMARA = timezone(timedelta(hours=4))

HEADER = """# WORK_LOG — Фенрир Терминал (автожурнал v1.4)
#
# Каждая выполненная задача сама пишет сюда строку. Формат v1.4:
# Дата | Время | Исполнитель | Задача | Статус | Примечания | Комментарий (авто)
# Ручные правки разрешены; авто-записи помечены [авто].
"""

MAX_TASK_LEN = 120
MAX_NOTE_LEN = 200
DEDUP_TAIL_LINES = 200


def default_path():
    """Путь журнала по умолчанию (рядом с модулем)."""
    env = os.environ.get("FENRIR_WORKLOG")
    if env:
        return os.path.expanduser(env)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "WORK_LOG.md")


def _clean(value, max_len):
    """Одна строка без разделителей и переводов строк."""
    text = str(value or "").replace("|", "/").replace("\n", " ")
    text = " ".join(text.split())
    if len(text) > max_len:
        text = text[:max_len - 1] + "…"
    return text


def format_entry(actor, task, status, note, when=None):
    """Строка журлога (без \\n). when — datetime, по умолчанию сейчас."""
    now = when or datetime.now(SAMARA)
    return (f"{now.strftime('%Y-%m-%d')} | {now.strftime('%H:%M')} | "
            f"{_clean(actor, 20)} | {_clean(task, MAX_TASK_LEN)} | "
            f"{_clean(status, 10)} | {_clean(note, MAX_NOTE_LEN)} | [авто]")


def _tail_lines(path, limit=DEDUP_TAIL_LINES):
    """Последние limit строк файла (для дедупа)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    return [ln.rstrip("\n") for ln in lines[-limit:]]


def worklog(actor, task, status, note, when=None, path=None):
    """Дописать строку в WORK_LOG.md. Возвращает True, если записано.

    False — если строка уже есть (дубль) или запись не удалась.
    Никогда не бросает исключений наружу.
    """
    path = path or default_path()
    line = format_entry(actor, task, status, note, when)
    try:
        if line in _tail_lines(path):
            return False
        is_new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if is_new:
                f.write(HEADER + "\n")
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except OSError:
        return False
