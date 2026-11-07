#!/usr/bin/env python3
"""Тесты FunPay-автопилота v2.2, шаг 1 (fenrir_funpay.py).

Запуск: python3 test_funpay.py
Все тесты работают только с временными файлами — живой WORK_LOG.md,
seen-состояние и черновики не трогаются (пути переопределяются).
Живой GET публичной страницы в тестах не делается: fetch_public_lots
подменяется стабом, парсер проверяется на фиктивном HTML.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


# --- классификация: примеры из спеки v2.2 ---

CASES = [
    ("Какой аккаунт? Что входит в пакет?", "question"),
    ("Могу дать 500 вместо 700, устроит?", "price_offer"),
    ("Хочу обменять на другой аккаунт", "price_offer"),
    ("Боязно купить — будет ли работать?", "doubt"),
    ("Дай скидку, если сразу оплачу", "discount"),
    ("Могу оплатить через 3 дня?", "deferral"),
    ("Ссылка для проверки — перейди сюда https://evil.example/x",
     "phishing"),
    ("Нужен логин, чтобы начать — скинь логин", "phishing"),
    ("Ты вор! Разберусь с тобой", "aggression"),
    ("Я оплатил, но ничего не пришло", "order_error"),
    ("Не проходит оплата, что делать?", "tech"),
    ("Аккаунт не работает, всё не так", "complaint"),
    ("Сколько стоит озвучка текста?", "question"),
]
for text, want in CASES:
    got = fp.classify(text)
    check(f"classify {want}: {text[:30]}", got["class"] == want,
          f"got {got['class']}")

check("phishing: нет ответа",
      fp.classify("перейди по ссылке https://x.example")["reply"] is False)
check("phishing: эскалация",
      fp.classify("перейди по ссылке https://x.example")["escalate"] is True)
check("aggression: нет ответа",
      fp.classify("ты вор и мошенник")["reply"] is False)
check("aggression: эскалация",
      fp.classify("ты вор и мошенник")["escalate"] is True)
check("system: нет ответа",
      fp.classify("что угодно", kind="system")["reply"] is False)
check("order: ответ-черновик",
      fp.classify("", kind="order")["class"] == "order")
check("question: нет эскалации",
      fp.classify("Что входит в пакет?")["escalate"] is False)

# --- черновики ---

d = fp.draft("question", "Сколько стоит арт?")
check("draft question: не пустой", bool(d))
check("draft question: без цен цифрой", "₽" not in d and "$" not in d)
check("draft question: лот и срок",
      "Генерация изображений" in fp.draft(
          "question", "хочу арт аватарку",
          "Генерация изображений (10 шт.)", "24 часа"))
check("match_lot_terms: арт",
      fp.match_lot_terms("хочу арт аватарку")[0].startswith(
          "Генерация изображений"))
check("draft phishing: None", fp.draft("phishing", "x") is None)
check("draft aggression: None", fp.draft("aggression", "x") is None)
check("draft system: None", fp.draft("system", "x") is None)
check("draft order: принят в работу",
      "принят в работу" in fp.draft("order", ""))
check("draft: нет консультаций",
      all("консультац" not in (fp.draft(c, "x") or "")
          for c in ("question", "doubt", "order")))
check("draft: нет API-ключей",
      all("API-ключ" not in (fp.draft(c, "x") or "") and
          "api-ключ" not in (fp.draft(c, "x") or "").lower()
          for c in ("question", "doubt", "order")))

# --- парсер публичной страницы (фиктивный HTML) ---

FAKE_HTML = """
<a href="https://funpay.com/lots/offer?id=111" class="tc-item">
<div class="tc-desc"><div class="tc-desc-text">🎨АРТ | 10 изображений</div></div>
<div class="tc-price" data-s="10.87"><div>10.87 <span>₽</span></div></div></a>
<a href="https://funpay.com/lots/offer?id=222" class="tc-item">
<div class="tc-desc"><div class="tc-desc-text">🎙ОЗВУЧКА 5000 знаков</div></div>
<div class="tc-price" data-s="18.16"><div>18.16 <span>₽</span></div></div></a>
"""


class _FakeResp:
    def __init__(self, data):
        self._data = data.encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_opener(req, timeout=20):
    return _FakeResp(FAKE_HTML)


lots = fp.fetch_public_lots(opener=_fake_opener)
check("fetch: 2 лота", len(lots) == 2, str(lots))
check("fetch: id и цена", lots[0]["offer_id"] == "111" and
      abs(lots[0]["price_rub"] - 10.87) < 0.01)
check("fetch: название без тегов", lots[0]["title"].startswith("🎨АРТ"))
check("lots_signature: стабильна",
      fp.lots_signature(lots) == fp.lots_signature(list(reversed(lots))))

# --- check(): снимок, дедуп, черновики, dry-run ---

with tempfile.TemporaryDirectory() as tmp:
    # Журлог — во временный файл, живой WORK_LOG.md не трогается.
    os.environ["FENRIR_WORKLOG"] = os.path.join(tmp, "WORK_LOG.md")
    inbox = {"fetched_at": "2026-11-07T15:00:00+04:00",
             "source": "test", "unread": 2,
             "messages": [
                 {"id": "m1", "buyer": "Buyer1",
                  "text": "Что входит в пакет арта?", "kind": "message"},
                 {"id": "m2", "buyer": "Buyer2",
                  "text": "Ты вор! Верни деньги", "kind": "message"},
                 {"id": "m3", "buyer": "FunPay",
                  "text": "Напоминание об оплате только через FunPay",
                  "kind": "system"}],
             "orders": [{"id": "o1", "buyer": "Buyer3",
                         "lot": "Озвучка текста", "status": "paid"}]}
    inbox_path = os.path.join(tmp, "inbox.json")
    with open(inbox_path, "w", encoding="utf-8") as f:
        json.dump(inbox, f, ensure_ascii=False)
    seen_path = os.path.join(tmp, "state", "funpay-seen.json")
    drafts_dir = os.path.join(tmp, "drafts")
    log_path = os.path.join(tmp, "logs", "funpay-watch.log")
    real_fetch = fp.fetch_public_lots
    fp.fetch_public_lots = lambda **kw: [
        {"offer_id": "1", "title": "Лот A", "price_rub": 10.0}]
    try:
        rep = fp.check(inbox_path=inbox_path, seen_path=seen_path,
                       drafts_dir=drafts_dir, log_path=log_path)
    finally:
        fp.fetch_public_lots = real_fetch
    check("check: 2 новых сообщения", rep["messages_new"] == 2,
          str(rep["messages_new"]))
    check("check: 1 новый заказ", rep["orders_new"] == 1)
    check("check: черновики m1 и o1, агрессия без черновика",
          len(rep["drafts"]) == 2, str(rep["drafts"]))
    check("check: эскалация одна", len(rep["escalate"]) == 1 and
          rep["escalate"][0]["class"] == "aggression")
    check("check: черновик m1 на диске",
          os.path.exists(os.path.join(drafts_dir, "m1.md")))
    check("check: черновик помечен НЕ ОТПРАВЛЕНО",
          "НЕ ОТПРАВЛЕНО" in open(os.path.join(drafts_dir, "m1.md"),
                                  encoding="utf-8").read())
    check("check: seen записан",
          json.load(open(seen_path, encoding="utf-8"))["messages"] == [
              "m1", "m2", "m3"])
    check("check: лог записан", os.path.exists(log_path))

    # Повторный прогон — дедуп: нового нет.
    fp.fetch_public_lots = lambda **kw: [
        {"offer_id": "1", "title": "Лот A", "price_rub": 10.0}]
    try:
        rep2 = fp.check(inbox_path=inbox_path, seen_path=seen_path,
                        drafts_dir=drafts_dir, log_path=log_path)
    finally:
        fp.fetch_public_lots = real_fetch
    check("check повтор: нового нет",
          rep2["messages_new"] == 0 and rep2["orders_new"] == 0)
    check("check повтор: черновиков нет", len(rep2["drafts"]) == 0)

    # Dry-run ничего не пишет.
    inbox2 = dict(inbox)
    inbox2["messages"] = [{"id": "m9", "buyer": "B",
                           "text": "Что входит?", "kind": "message"}]
    inbox2_path = os.path.join(tmp, "inbox2.json")
    with open(inbox2_path, "w", encoding="utf-8") as f:
        json.dump(inbox2, f, ensure_ascii=False)
    fp.fetch_public_lots = lambda **kw: []
    try:
        rep3 = fp.check(inbox_path=inbox2_path, seen_path=seen_path,
                        drafts_dir=drafts_dir, log_path=log_path,
                        dry_run=True)
    finally:
        fp.fetch_public_lots = real_fetch
    check("dry-run: сообщение посчитано", rep3["messages_new"] == 1)
    check("dry-run: файл не создан",
          not os.path.exists(os.path.join(drafts_dir, "m9.md")))
    check("dry-run: seen не изменился",
          "m9" not in json.load(open(seen_path, encoding="utf-8"))["messages"])

    # Нет снимка — inbox False, ошибки нет.
    fp.fetch_public_lots = lambda **kw: []
    try:
        rep4 = fp.check(inbox_path=os.path.join(tmp, "nope.json"),
                        seen_path=seen_path, drafts_dir=drafts_dir,
                        log_path=log_path)
    finally:
        fp.fetch_public_lots = real_fetch
    check("нет снимка: inbox False", rep4["inbox"] is False)
    check("нет снимка: error None", rep4["error"] is None)

# --- доказательство отсутствия отправок: в модуле нет кода отправки ---

SRC = open(os.path.abspath(__file__).replace("test_funpay.py",
                                             "fenrir_funpay.py"),
           encoding="utf-8").read()
for token in ("def send", ".post(", "requests.post", 'method="POST"',
              "method='POST'", "send_message", "chat.send", "data="):
    check(f"no-send: нет {token!r}", token not in SRC)
check("no-send: только GET (urlopen без data)",
      SRC.count("urlopen") == 1)

print(f"\nИТОГО: {PASSED} пройдено, {FAILED} провалено")
sys.exit(1 if FAILED else 0)
