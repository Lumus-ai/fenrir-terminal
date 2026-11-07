#!/usr/bin/env python3
"""Тесты FunPay-автопилота v2.2, шаг 2 (очередь + выполнение заказов).

Запуск: python3 test_funpay_queue.py
Все тесты работают только с временными файлами — живой WORK_LOG.md,
seen-состояние, черновики, outbox и заказы не трогаются (пути
переопределяются, FENRIR_WORKLOG указывает во временный файл).
Живой GET и живой агент в тестах не вызываются: fetch_public_lots
и выполнение заказов подменяются стабами.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="funpay-q2-")
os.environ["FENRIR_WORKLOG"] = os.path.join(_TMP, "WORK_LOG.md")

import fenrir_funpay as fp  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, cond, extra=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        print(f"FAIL: {name} {extra}")


def paths(name):
    d = os.path.join(_TMP, name)
    os.makedirs(d, exist_ok=True)
    return {
        "drafts": os.path.join(d, "drafts"),
        "outbox": os.path.join(d, "outbox.json"),
        "seen": os.path.join(d, "seen.json"),
        "inbox": os.path.join(d, "inbox.json"),
        "orders": os.path.join(d, "orders"),
        "log": os.path.join(d, "watch.log"),
    }


def write_inbox(p, orders, messages=None):
    data = {"fetched_at": "2026-11-07T16:00:00+04:00",
            "source": "test", "unread": len(messages or []),
            "messages": messages or [], "orders": orders}
    with open(p["inbox"], "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# --- parse_draft ---

p = paths("parse")
fp.write_draft(p["drafts"], "m42", "Buyer1", "question", "Привет, текст")
parsed = fp.parse_draft(os.path.join(p["drafts"], "m42.md"))
check("parse_draft: id", parsed and parsed["id"] == "m42")
check("parse_draft: buyer", parsed and parsed["buyer"] == "Buyer1")
check("parse_draft: class", parsed and parsed["class"] == "question")
check("parse_draft: text", parsed and parsed["text"] == "Привет, текст")
check("parse_draft: нет файла",
      fp.parse_draft(os.path.join(p["drafts"], "nope.md")) is None)
broken = os.path.join(p["drafts"], "broken.md")
with open(broken, "w", encoding="utf-8") as f:
    f.write("мусор без класса и текста")
check("parse_draft: битый файл",
      fp.parse_draft(broken) is None)

# --- enqueue: ok, дедуп, запреты ---

p = paths("enqueue")
fp.write_draft(p["drafts"], "m1", "Buyer1", "question", "Ответ покупателю")
res = fp.enqueue("m1", drafts_dir=p["drafts"], outbox_path=p["outbox"],
                 log_path=p["log"])
check("enqueue: ok", res["ok"], str(res))
check("enqueue: статус queued",
      res["ok"] and res["entry"]["status"] == "queued")
check("enqueue: message_id",
      res["ok"] and res["entry"]["message_id"] == "m1")
check("enqueue: order_id пуст для сообщения",
      res["ok"] and res["entry"]["order_id"] is None)
check("enqueue: текст в очереди",
      res["ok"] and res["entry"]["text"] == "Ответ покупателю")
res2 = fp.enqueue("m1", drafts_dir=p["drafts"], outbox_path=p["outbox"],
                  log_path=p["log"])
check("enqueue: дедуп", res2["ok"] and res2.get("duplicate") is True)
check("enqueue: одна запись после дедупа",
      len(fp.queue_list(p["outbox"])) == 1)

fp.write_draft(p["drafts"], "o9", "Buyer2", "order", "Заказ принят")
res3 = fp.enqueue("o9", drafts_dir=p["drafts"], outbox_path=p["outbox"],
                  log_path=p["log"])
check("enqueue: заказ — order_id",
      res3["ok"] and res3["entry"]["order_id"] == "o9")
check("enqueue: заказ — message_id пуст",
      res3["ok"] and res3["entry"]["message_id"] is None)

for bad_class in ("phishing", "aggression", "system"):
    fp.write_draft(p["drafts"], f"bad-{bad_class}", "Evil",
                   bad_class, "текст")
    rb = fp.enqueue(f"bad-{bad_class}", drafts_dir=p["drafts"],
                    outbox_path=p["outbox"], log_path=p["log"])
    check(f"enqueue: запрет {bad_class}",
          rb["ok"] is False and "не ставится" in rb["error"], str(rb))
check("enqueue: запрещённые не в очереди",
      len(fp.queue_list(p["outbox"])) == 2)
res4 = fp.enqueue("нет-такого", drafts_dir=p["drafts"],
                  outbox_path=p["outbox"], log_path=p["log"])
check("enqueue: нет черновика", res4["ok"] is False)

# --- check --enqueue: интеграция ---

p = paths("check-enq")
fp.fetch_public_lots = lambda: []
inbox = {"fetched_at": "2026-11-07T16:00:00+04:00", "source": "test",
         "unread": 1,
         "messages": [{"id": "m100", "buyer": "Buyer3",
                       "text": "Сколько стоит озвучка текста?",
                       "time": "2026-11-07 16:00", "order_id": None,
                       "kind": "message"}],
         "orders": []}
with open(p["inbox"], "w", encoding="utf-8") as f:
    json.dump(inbox, f, ensure_ascii=False)
rep = fp.check(inbox_path=p["inbox"], seen_path=p["seen"],
               drafts_dir=p["drafts"], log_path=p["log"],
               outbox_path=p["outbox"], enqueue_drafts=True)
check("check --enqueue: черновик создан", len(rep["drafts"]) == 1)
check("check --enqueue: в очереди", rep["queued"] == ["m100"],
      str(rep["queued"]))
check("check --enqueue: запись queued",
      len(fp.queue_list(p["outbox"])) == 1)
rep2 = fp.check(inbox_path=p["inbox"], seen_path=p["seen"],
                drafts_dir=p["drafts"], log_path=p["log"],
                outbox_path=p["outbox"], enqueue_drafts=False)
check("check без --enqueue: новых нет", rep2["messages_new"] == 0)

# --- fulfill_kind ---

check("fulfill_kind: тексты",
      fp.fulfill_kind("✍【ТЕКСТЫ НА ЗАКАЗ】Рерайт, копирайт")[0] == "text")
check("fulfill_kind: код",
      fp.fulfill_kind("💻【ПОМОЩЬ С КОДОМ】Python/JS")[0] == "code")
check("fulfill_kind: песня",
      fp.fulfill_kind("🎵【ТЕКСТ ПЕСНИ】Напишу текст + теги")[0] == "text")
check("fulfill_kind: chatgpt",
      fp.fulfill_kind("⚡【5 ЗАПРОСОВ В CHATGPT】Выполню за вас")[0] == "text")
for lot in ("🎨【10 AI-ИЗОБРАЖЕНИЙ】Генерация по описанию",
            "🎙【ОЗВУЧКА НЕЙРОГОЛОСОМ】Текст до 5000 знаков",
            "🔍【АПСКЕЙЛ ФОТО ДО 4K】Улучшение качества",
            "🛒【ДИЗАЙН КАРТОЧКИ WB/OZON】Инфографика",
            "🎧【АУДИО-САММАРИ】Озвучка текста / мини-подкаст",
            "неизвестный лот"):
    check(f"fulfill_kind: не выполняется — {lot[:20]}",
          fp.fulfill_kind(lot) is None)

# --- fulfill: стабы агента ---

CALLS = []


def stub_ok(kind, task, workdir):
    CALLS.append((kind, task, workdir))
    os.makedirs(workdir, exist_ok=True)
    with open(os.path.join(workdir, "result.txt"), "w",
              encoding="utf-8") as f:
        f.write("тестовый результат заказа")
    return True, "stub/model", []


def stub_fail(kind, task, workdir):
    CALLS.append((kind, task, workdir))
    os.makedirs(workdir, exist_ok=True)
    return False, "stub/model", []


def stub_no_files(kind, task, workdir):
    CALLS.append((kind, task, workdir))
    os.makedirs(workdir, exist_ok=True)
    return True, "stub/model", []


def order(oid, lot, status="paid", task="Сделай: короткий текст."):
    return {"id": oid, "buyer": "BuyerX", "lot": lot, "status": status,
            "time": "2026-11-07 16:00", "task": task}


p = paths("fulfill-ok")
write_inbox(p, [order("ord-1", "✍【ТЕКСТЫ НА ЗАКАЗ】Рерайт, копирайт")])
CALLS.clear()
rep = fp.fulfill(inbox_path=p["inbox"], seen_path=p["seen"],
                 outbox_path=p["outbox"], orders_dir=p["orders"],
                 log_path=p["log"], run_agent=stub_ok)
check("fulfill: выполнен", len(rep["fulfilled"]) == 1, str(rep))
check("fulfill: агент вызван с kind text",
      CALLS and CALLS[0][0] == "text")
check("fulfill: файл результата на диске",
      os.path.isfile(os.path.join(p["orders"], "ord-1", "result.txt")))
entries = fp.queue_list(p["outbox"])
check("fulfill: запись сдачи в очереди", len(entries) == 1)
check("fulfill: сдача — kind delivery",
      entries and entries[0]["kind"] == "delivery")
check("fulfill: сдача — статус queued",
      entries and entries[0]["status"] == "queued")
check("fulfill: сдача — order_id",
      entries and entries[0]["order_id"] == "ord-1")
check("fulfill: сдача — файл указан",
      entries and len(entries[0]["files"]) == 1)
rep2 = fp.fulfill(inbox_path=p["inbox"], seen_path=p["seen"],
                  outbox_path=p["outbox"], orders_dir=p["orders"],
                  log_path=p["log"], run_agent=stub_ok)
check("fulfill: повтор — пропуск, без дублей",
      rep2["skipped"] and len(fp.queue_list(p["outbox"])) == 1,
      str(rep2))

p = paths("fulfill-art")
write_inbox(p, [order("ord-2", "🎨【10 AI-ИЗОБРАЖЕНИЙ】Генерация")])
CALLS.clear()
rep = fp.fulfill(inbox_path=p["inbox"], seen_path=p["seen"],
                 outbox_path=p["outbox"], orders_dir=p["orders"],
                 log_path=p["log"], run_agent=stub_ok)
check("fulfill: арт — needs_owner", len(rep["needs_owner"]) == 1)
check("fulfill: арт — агент не вызван", len(CALLS) == 0)
check("fulfill: арт — очередь пуста",
      len(fp.queue_list(p["outbox"])) == 0)

p = paths("fulfill-notask")
o = order("ord-3", "✍【ТЕКСТЫ НА ЗАКАЗ】Рерайт, копирайт")
o.pop("task")
write_inbox(p, [o])
CALLS.clear()
rep = fp.fulfill(inbox_path=p["inbox"], seen_path=p["seen"],
                 outbox_path=p["outbox"], orders_dir=p["orders"],
                 log_path=p["log"], run_agent=stub_ok)
check("fulfill: без ТЗ — needs_owner", len(rep["needs_owner"]) == 1)
check("fulfill: без ТЗ — агент не вызван", len(CALLS) == 0)

p = paths("fulfill-unpaid")
write_inbox(p, [order("ord-4", "✍【ТЕКСТЫ НА ЗАКАЗ】Рерайт, копирайт",
                      status="created")])
CALLS.clear()
rep = fp.fulfill(inbox_path=p["inbox"], seen_path=p["seen"],
                 outbox_path=p["outbox"], orders_dir=p["orders"],
                 log_path=p["log"], run_agent=stub_ok)
check("fulfill: не оплачен — пропуск", len(rep["skipped"]) == 1)
check("fulfill: не оплачен — агент не вызван", len(CALLS) == 0)

p = paths("fulfill-fail")
write_inbox(p, [order("ord-5", "💻【ПОМОЩЬ С КОДОМ】Python/JS")])
rep = fp.fulfill(inbox_path=p["inbox"], seen_path=p["seen"],
                 outbox_path=p["outbox"], orders_dir=p["orders"],
                 log_path=p["log"], run_agent=stub_fail)
check("fulfill: провал агента — needs_owner",
      len(rep["needs_owner"]) == 1)
check("fulfill: провал — очередь пуста",
      len(fp.queue_list(p["outbox"])) == 0)

p = paths("fulfill-nofiles")
write_inbox(p, [order("ord-6", "🎵【ТЕКСТ ПЕСНИ】Напишу текст + теги")])
rep = fp.fulfill(inbox_path=p["inbox"], seen_path=p["seen"],
                 outbox_path=p["outbox"], orders_dir=p["orders"],
                 log_path=p["log"], run_agent=stub_no_files)
check("fulfill: нет файлов — needs_owner",
      len(rep["needs_owner"]) == 1
      and "файлов нет" in rep["needs_owner"][0][1])

p = paths("fulfill-dry")
write_inbox(p, [order("ord-7", "✍【ТЕКСТЫ НА ЗАКАЗ】Рерайт, копирайт")])
CALLS.clear()
rep = fp.fulfill(inbox_path=p["inbox"], seen_path=p["seen"],
                 outbox_path=p["outbox"], orders_dir=p["orders"],
                 log_path=p["log"], dry_run=True, run_agent=stub_ok)
check("fulfill: dry-run — агент не вызван", len(CALLS) == 0)
check("fulfill: dry-run — очередь пуста",
      len(fp.queue_list(p["outbox"])) == 0)
check("fulfill: dry-run — папка заказа не создана",
      not os.path.isdir(os.path.join(p["orders"], "ord-7")))

p = paths("fulfill-order-filter")
write_inbox(p, [order("ord-8", "✍【ТЕКСТЫ НА ЗАКАЗ】Рерайт, копирайт"),
                order("ord-9", "⚡【5 ЗАПРОСОВ В CHATGPT】Выполню за вас")])
rep = fp.fulfill(inbox_path=p["inbox"], seen_path=p["seen"],
                 outbox_path=p["outbox"], orders_dir=p["orders"],
                 log_path=p["log"], order_id="ord-9", run_agent=stub_ok)
check("fulfill: --order — только один",
      len(rep["fulfilled"]) == 1 and rep["fulfilled"][0][0] == "ord-9",
      str(rep))

rep = fp.fulfill(inbox_path=os.path.join(_TMP, "нет.json"),
                 seen_path=p["seen"], outbox_path=p["outbox"],
                 orders_dir=p["orders"], log_path=p["log"],
                 run_agent=stub_ok)
check("fulfill: нет снимка — ошибка", rep["error"] is not None)

# --- в коде нет отправки на FunPay ---

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fenrir_funpay.py"), encoding="utf-8").read()
for pat in ("def send", "send_message", ".post(", "method=\"POST\"",
            "method='POST'", "requests.post", "urlopen(req, data"):
    check(f"нет отправки: {pat}", pat not in src)

print(f"\nИТОГО: {PASSED} пройдено, {FAILED} провалено")
sys.exit(1 if FAILED else 0)
