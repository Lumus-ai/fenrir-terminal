#!/usr/bin/env python3
"""Fenrir Terminal v2.2 — FunPay-автопилот, ШАГ 1: только чтение и классификация.

Модуль ЧИТАЕТ состояние магазина и готовит ЧЕРНОВИКИ ответов в файл.
Он НЕ отправляет сообщения, НЕ поднимает лоты, НЕ выполняет заказы,
НЕ меняет цены и НЕ трогает баланс. Отправка и выполнение — отдельные
шаги, только после слова Shadow. В коде нет ни одной функции отправки:
нет POST-запросов, нет вызовов чата, нет записи на FunPay.

Маршруты чтения (два, оба read-only):
1. Публичная страница продавца (https://funpay.com/users/22096936/) —
   обычный GET без логина и кук, через urllib с обычным User-Agent.
   Даёт живой список лотов: названия и цены. Сообщений там нет.
2. Снимок входящих (inbox.json) — JSON, который пишет браузерный
   вотч (живой браузер с Google-сессией, крон funpay-watch-messages):
   непрочитанные сообщения и новые заказы. Модуль только читает файл.
   Формат снимка:
     {"fetched_at": "2026-11-07T15:30:00+04:00",
      "source": "browser-watch",
      "unread": 0,
      "messages": [{"id": "m1", "buyer": "Nick", "text": "...",
                    "time": "2026-11-07 15:00", "order_id": null,
                    "kind": "message"}],
      "orders": [{"id": "o1", "buyer": "Nick", "lot": "…",
                  "status": "paid", "time": "2026-11-07 15:00"}]}
   kind: "message" | "order" | "system". Системные уведомления FunPay
   классифицируются как system и черновиков не получают.

Классификация — по спеке v2.2 (PLAN-EXTENDED.md, «Классификация
сообщений FunPay v2.2», 10 классов) + два служебных класса:
  order  — новый оплаченный заказ (из снимка, kind=order);
  system — системное уведомление FunPay (без действий).
Порядок проверок: фишинг и агрессия — первыми (на них нет ответа,
только блокировка + жалоба + эскалация).

Правила магазина Shadow (не нарушать, вшиты в черновики):
- Консультаций нет (лот 8 «Час вопросов-ответов» не выставлен и не
  предлагается; в каталоге модуля его нет).
- API-лоты на паузе (ключи/токены не продаются и не предлагаются).
- Цены ТОЛЬКО в USD; черновики цену цифрой не называют — только
  «цена указана на странице лота», чтобы не выдумать курс.
- Оплата — только через FunPay, до начала выполнения.

Состояние: <папка модуля>/state/funpay-seen.json — id уже обработанных
сообщений/заказов и сигнатуры лотов (для дедупа и обнаружения
новых/снятых лотов). Черновики: ~/workspace/funpay-shop/drafts/<id>.md.
Журнал проверок: <папка модуля>/logs/funpay-watch.log.
Одна строка на проверку — в WORK_LOG.md через fenrir_worklog.

CLI (через bin/fenrir):
  fenrir funpay check [--inbox PATH] [--file PATH] [--dry-run]
  fenrir funpay status [--inbox PATH]
--file — классифицировать сообщения из произвольного JSON-файла
(тот же формат снимка), не трогая seen-состояние и WORK_LOG.
--dry-run — только показать, что было бы сделано, ничего не писать.

Запуск тестов: python3 test_funpay.py
"""
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from zoneinfo import ZoneInfo
    SAMARA = ZoneInfo("Europe/Samara")
except Exception:  # pragma: no cover - нет tzdata
    SAMARA = timezone(timedelta(hours=4))

SHOP_NICK = "Fenrirofdrakons"
SHOP_ID = "22096936"
PROFILE_URL = "https://funpay.com/users/22096936/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
USD_RATE = 81.8  # справочно; черновики цены цифрой не называют

# Каталог выставленных лотов: срок выполнения — из lots_draft.md.
# Лот 8 (консультация «Час вопросов-ответов») НЕ выставлен и в каталоге
# отсутствует намеренно: консультаций нет (правило Shadow).
# Ключи — по названию лота, для подбора срока в черновике.
LOT_TERMS = [
    (("арт", "аватар", "обложк", "иллюстрац", "изображен"),
     "Генерация изображений (10 шт.)", "24 часа"),
    (("озвуч", "нейроголос", "голос"),
     "Озвучка нейроголосом", "24 часа"),
    (("апскейл", "чёткост", "качеств", "фото"),
     "Улучшение качества фото (апскейл)", "12 часов"),
    (("инфографик", "карточк", "слайд"),
     "Инфографика слайда для WB/Ozon", "48 часов, 2 круга правок"),
    (("рерайт", "копирайт", "перевод", "текст"),
     "Тексты на заказ (рерайт, копирайт, переводы)", "24 часа"),
    (("код", "python", "js", "скрипт", "ошибк", "cursorrules", "cursor"),
     "Помощь с кодом / настройка Cursor", "24 часа"),
    (("песн", "текст песни", "suno"),
     "Текст песни + теги для Suno", "24 часа"),
    (("подкаст", "саммари", "аудио"),
     "Озвучка текста / мини-подкаст", "24 часа"),
    (("chatgpt", "запрос"),
     "Выполнение запросов в ChatGPT (5 шт.)", "пара часов"),
]

# Классы классификации (спека v2.2, 10 классов + order/system).
# action: answer — готовить черновик; none — не отвечать.
CLASSES = {
    "question": "Обычный запрос (вопрос по товару)",
    "price_offer": "Предложение по цене / бартер",
    "doubt": "Сомнения / страхи покупателя",
    "discount": "Требование скидки / бонусов",
    "deferral": "Запрос на отсрочку / рассрочку",
    "phishing": "Подозрительные действия / фишинг",
    "aggression": "Агрессия / оскорбления / угрозы",
    "order_error": "Ошибки в заказе / непонимание процесса",
    "tech": "Технические проблемы (платёж, система)",
    "complaint": "Жалобы / претензии после получения",
    "order": "Новый оплаченный заказ",
    "system": "Системное уведомление FunPay",
}

_AGGRESSION = ("вор", "мошенник", "кинул", "разберусь", "убью", "сволоч",
               "тварь", "обманул", "аферист", "верни деньги, сука")


def _has(text, words):
    return any(w in text for w in words)


def classify(text, kind="message"):
    """Классифицировать одно сообщение. Возвращает dict.

    kind: message | order | system (из снимка; order/system имеют
    приоритет над текстом).
    """
    if kind == "system":
        return {"class": "system", "label": CLASSES["system"],
                "action": "none", "escalate": False, "reply": False}
    if kind == "order":
        return {"class": "order", "label": CLASSES["order"],
                "action": "answer", "escalate": False, "reply": True}
    t = (text or "").lower()
    aggressive = _has(t, _AGGRESSION)
    # Фишинг и агрессия — первыми, на них нет ответа.
    if re.search(r"https?://", t) and _has(
            t, ("провер", "перейд", "войд", "подтверд", "забер", "получи")):
        return {"class": "phishing", "label": CLASSES["phishing"],
                "action": "block", "escalate": True, "reply": False}
    if _has(t, ("логин", "пароль")) and _has(t, ("дай", "скинь", "нужен",
                                                 "пришли", "скажи")):
        return {"class": "phishing", "label": CLASSES["phishing"],
                "action": "block", "escalate": True, "reply": False}
    if aggressive:
        return {"class": "aggression", "label": CLASSES["aggression"],
                "action": "block", "escalate": True, "reply": False}
    if _has(t, ("оплатил", "оплатила", "не пришло", "не пришли",
                "где мой заказ", "где товар", "не получил", "оплата прошла")):
        return {"class": "order_error", "label": CLASSES["order_error"],
                "action": "answer", "escalate": False, "reply": True}
    if _has(t, ("не работает", "всё не так", "все не так", "возврат",
                "верните", "недоволен", "недовольна", "плохо сделали",
                "не то")):
        return {"class": "complaint", "label": CLASSES["complaint"],
                "action": "answer", "escalate": False, "reply": True}
    if _has(t, ("не проходит оплата", "не могу оплатить",
                "ошибка оплаты", "не оплачивается", "не проходит платёж",
                "не проходит платеж")):
        return {"class": "tech", "label": CLASSES["tech"],
                "action": "answer", "escalate": False, "reply": True}
    if _has(t, ("скидк", "бонус")):
        return {"class": "discount", "label": CLASSES["discount"],
                "action": "answer", "escalate": aggressive, "reply": True}
    if _has(t, ("вместо", "обмен", "бартер", "дешевле", "уступи",
                "торг")):
        return {"class": "price_offer", "label": CLASSES["price_offer"],
                "action": "answer", "escalate": aggressive, "reply": True}
    if _has(t, ("рассрочк", "отсрочк", "потом оплачу", "оплачу позже",
                "через 3 дня", "через неделю оплачу", "в долг")):
        return {"class": "deferral", "label": CLASSES["deferral"],
                "action": "answer", "escalate": aggressive, "reply": True}
    if _has(t, ("боюсь", "боязно", "не обман", "точно работает",
                "гаранти", "страшно")):
        return {"class": "doubt", "label": CLASSES["doubt"],
                "action": "answer", "escalate": aggressive, "reply": True}
    return {"class": "question", "label": CLASSES["question"],
            "action": "answer", "escalate": False, "reply": True}


def match_lot_terms(text):
    """Подобрать лот по тексту сообщения. Возвращает (название, срок)
    или (None, None)."""
    t = (text or "").lower()
    for keys, name, term in LOT_TERMS:
        if _has(t, keys):
            return name, term
    return None, None


def draft(class_id, message, lot_name=None, lot_term=None):
    """Черновик ответа продавца (НЕ отправка). Возвращает текст или None,
    если отвечать не нужно (фишинг, агрессия, системное)."""
    lot_line = ""
    if lot_name:
        lot_line = f"Лот: {lot_name}. Срок выполнения — {lot_term}. "
    if class_id == "question":
        return ("Здравствуйте! " + lot_line +
                "Цена и условия указаны на странице лота, оплата — только "
                "через FunPay. Напишите, что именно нужно, — подскажу по "
                "срокам и деталям.")
    if class_id == "price_offer":
        return ("Здравствуйте! Цена лота фиксированная — торг и обмен не "
                "предусмотрены. Если цена подходит — оформляйте заказ, "
                "выполню в срок.")
    if class_id == "doubt":
        return ("Здравствуйте! Понимаю сомнения. Работаю через FunPay: "
                "оплата проходит через площадку, заказ выполняется в чате "
                "заказа в указанный срок, отзывы — на странице профиля. "
                "Если что-то пойдёт не так — разбираемся через поддержку "
                "FunPay.")
    if class_id == "discount":
        return ("Здравствуйте! Скидки не предусмотрены, цена лота "
                "фиксированная. При повторной покупке — бонусная работа "
                "по договорённости.")
    if class_id == "deferral":
        return ("Здравствуйте! Без оплаты начать не могу — оплата до начала "
                "выполнения, это правило площадки. Как будете готовы — "
                "оформляйте заказ.")
    if class_id == "order_error":
        return ("Здравствуйте! Проверяю статус вашего заказа. Напишите, "
                "пожалуйста, номер заказа — посмотрю, на каком он этапе, "
                "и отпишусь. Срок выполнения указан на странице лота.")
    if class_id == "tech":
        return ("Здравствуйте! Оплата проходит на стороне FunPay — "
                "попробуйте обновить страницу и повторить оплату чуть "
                "позже. Если не пройдёт — напишите в поддержку FunPay, "
                "они видят платёж.")
    if class_id == "complaint":
        return ("Здравствуйте! Разбираюсь: напишите номер заказа и что "
                "именно не так — проверю и предложу решение (исправление, "
                "замена или возврат по правилам площадки).")
    if class_id == "order":
        return ("Здравствуйте! Заказ принят в работу. " + lot_line +
                "Результат пришлю в чат заказа в срок. Если нужны "
                "уточнения по заданию — пишите сюда.")
    return None


# --- чтение публичной страницы магазина (GET, без логина) ---

_LOT_RE = re.compile(
    r'href="https://funpay\.com/lots/offer\?id=(\d+)"[^>]*>'
    r'.*?tc-desc-text">(.*?)</div>.*?data-s="([\d.]+)"', re.S)


def _clean_html(text):
    return re.sub(r"<[^>]+>", "", text).strip()


def fetch_public_lots(url=PROFILE_URL, timeout=20, opener=None):
    """Живой список лотов с публичной страницы. Возвращает список
    {offer_id, title, price_rub} или поднимает исключение при ошибке."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    open_fn = opener or urllib.request.urlopen
    with open_fn(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", "replace")
    lots = []
    for offer_id, title, price in _LOT_RE.findall(html):
        lots.append({"offer_id": offer_id,
                     "title": _clean_html(title),
                     "price_rub": float(price)})
    return lots


def lots_signature(lots):
    """Сигнатура списка лотов для сравнения (порядок не важен)."""
    keys = sorted(f"{l['offer_id']}:{l['title']}" for l in lots)
    return hashlib.sha1("\n".join(keys).encode("utf-8")).hexdigest()[:12]


# --- снимок входящих ---

def default_inbox_path():
    return os.path.expanduser("~/workspace/funpay-shop/inbox.json")


def load_inbox(path):
    """Прочитать снимок входящих. Возвращает dict или None (нет файла)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("messages", [])
    data.setdefault("orders", [])
    return data


# --- seen-состояние ---

def _term_dir():
    return os.path.dirname(os.path.abspath(__file__))


def default_seen_path():
    return os.path.join(_term_dir(), "state", "funpay-seen.json")


def load_seen(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"messages": [], "orders": [], "lots_sig": None,
                "last_check": None}
    if not isinstance(data, dict):
        return {"messages": [], "orders": [], "lots_sig": None,
                "last_check": None}
    data.setdefault("messages", [])
    data.setdefault("orders", [])
    return data


def save_seen(path, seen):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def default_log_path():
    return os.path.join(_term_dir(), "logs", "funpay-watch.log")


def _log(path, line):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now = datetime.now(SAMARA).strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{now} {line}\n")


def default_drafts_dir():
    return os.path.expanduser("~/workspace/funpay-shop/drafts")


def write_draft(drafts_dir, msg_id, buyer, class_id, text):
    """Записать черновик ответа в файл. Возвращает путь."""
    os.makedirs(drafts_dir, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(msg_id))[:40]
    path = os.path.join(drafts_dir, f"{safe_id}.md")
    body = (f"# Черновик ответа — {buyer}\n\n"
            f"Класс: {class_id} ({CLASSES.get(class_id, class_id)})\n"
            f"Статус: ЧЕРНОВИК, НЕ ОТПРАВЛЕНО\n\n{text}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def check(inbox_path=None, seen_path=None, drafts_dir=None,
          log_path=None, dry_run=False, now=None):
    """Одна проверка: живые лоты + снимок входящих.

    Возвращает dict-отчёт. Ничего не отправляет никуда.
    """
    inbox_path = inbox_path or default_inbox_path()
    seen_path = seen_path or default_seen_path()
    drafts_dir = drafts_dir or default_drafts_dir()
    log_path = log_path or default_log_path()
    now = now or datetime.now(SAMARA)
    report = {"lots": [], "lots_new": [], "lots_gone": [],
              "messages_new": 0, "orders_new": 0, "drafts": [],
              "escalate": [], "inbox": False, "error": None}
    seen = load_seen(seen_path)

    try:
        lots = fetch_public_lots()
        report["lots"] = lots
        sig = lots_signature(lots)
        old_sig = seen.get("lots_sig")
        if old_sig is None:
            report["lots_new"] = [l["title"] for l in lots]
            report["lots_note"] = "базовый снимок лотов"
        elif sig != old_sig:
            old_titles = set(seen.get("lots_titles", []))
            new_titles = {l["title"] for l in lots}
            report["lots_new"] = sorted(new_titles - old_titles)
            report["lots_gone"] = sorted(old_titles - new_titles)
        if not dry_run:
            seen["lots_sig"] = sig
            seen["lots_titles"] = sorted(l["title"] for l in lots)
    except Exception as e:  # noqa: BLE001 - живой GET может упасть
        report["error"] = f"публичная страница недоступна: {e}"
        lots = []

    inbox = load_inbox(inbox_path)
    if inbox is None:
        report["inbox"] = False
    else:
        report["inbox"] = True
        report["inbox_fetched_at"] = inbox.get("fetched_at")
        seen_msgs = set(seen.get("messages", []))
        seen_orders = set(seen.get("orders", []))
        for msg in inbox.get("messages", []):
            mid = str(msg.get("id", ""))
            if mid and mid in seen_msgs:
                continue
            result = classify(msg.get("text", ""), msg.get("kind", "message"))
            if result["class"] != "system":
                report["messages_new"] += 1
            if result["escalate"]:
                report["escalate"].append(
                    {"id": mid, "buyer": msg.get("buyer"),
                     "class": result["class"]})
            if result["reply"]:
                lot_name, lot_term = match_lot_terms(msg.get("text", ""))
                text = draft(result["class"], msg.get("text", ""),
                             lot_name, lot_term)
                if text and not dry_run:
                    path = write_draft(drafts_dir, mid or "noid",
                                       msg.get("buyer", "?"),
                                       result["class"], text)
                    report["drafts"].append(path)
                elif text:
                    report["drafts"].append(f"(dry-run) {mid}")
            if not dry_run and mid:
                seen["messages"].append(mid)
        for order in inbox.get("orders", []):
            oid = str(order.get("id", ""))
            if oid and oid in seen_orders:
                continue
            report["orders_new"] += 1
            lot_name, lot_term = match_lot_terms(order.get("lot", ""))
            text = draft("order", "", lot_name, lot_term)
            if text and not dry_run:
                path = write_draft(drafts_dir, oid or "noid",
                                   order.get("buyer", "?"), "order", text)
                report["drafts"].append(path)
            elif text:
                report["drafts"].append(f"(dry-run) {oid}")
            if not dry_run and oid:
                seen["orders"].append(oid)

    if not dry_run:
        seen["last_check"] = now.strftime("%Y-%m-%dT%H:%M:%S+04:00")
        save_seen(seen_path, seen)
        _log(log_path,
             f"check: лотов {len(report['lots'])}, новых сообщений "
             f"{report['messages_new']}, новых заказов "
             f"{report['orders_new']}, черновиков "
             f"{len(report['drafts'])}, эскалаций "
             f"{len(report['escalate'])}"
             + (f", ОШИБКА {report['error']}" if report["error"] else ""))
        try:
            from fenrir_worklog import worklog
            worklog("funpay",
                    "FunPay: проверка сообщений и заказов",
                    "ok" if not report["error"] else "failed",
                    f"лотов {len(report['lots'])}, новых сообщений "
                    f"{report['messages_new']}, новых заказов "
                    f"{report['orders_new']}, черновиков "
                    f"{len(report['drafts'])}"
                    + (f", {report['error']}" if report["error"] else ""))
        except Exception:  # noqa: BLE001 - журлог не роняет проверку
            pass
    return report


def status(inbox_path=None, seen_path=None):
    """Сводка: лоты, непрочитанные, новые заказы, последняя проверка."""
    inbox_path = inbox_path or default_inbox_path()
    seen_path = seen_path or default_seen_path()
    seen = load_seen(seen_path)
    inbox = load_inbox(inbox_path)
    unread = inbox.get("unread", 0) if inbox else None
    return {
        "lots_known": len(seen.get("lots_titles", [])),
        "unread": unread,
        "inbox_fetched_at": inbox.get("fetched_at") if inbox else None,
        "seen_messages": len(seen.get("messages", [])),
        "seen_orders": len(seen.get("orders", [])),
        "last_check": seen.get("last_check"),
    }


def main(argv):
    if not argv or argv[0] not in ("check", "status"):
        print("Использование: fenrir funpay check [--inbox PATH] "
              "[--file PATH] [--dry-run] | fenrir funpay status")
        return 2
    cmd = argv[0]
    inbox = None
    dry_run = False
    i = 1
    while i < len(argv):
        if argv[i] == "--inbox" and i + 1 < len(argv):
            inbox = argv[i + 1]
            i += 2
        elif argv[i] == "--file" and i + 1 < len(argv):
            inbox = argv[i + 1]
            i += 2
        elif argv[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            print(f"неизвестный аргумент: {argv[i]}")
            return 2
    if cmd == "status":
        st = status(inbox_path=inbox)
        print(f"FunPay {SHOP_NICK} (id {SHOP_ID})")
        print(f"Лотов известно: {st['lots_known']}")
        print(f"Непрочитанных: "
              f"{st['unread'] if st['unread'] is not None else 'нет снимка'}")
        print(f"Снимок входящих: {st['inbox_fetched_at'] or 'нет'}")
        print(f"Обработано сообщений: {st['seen_messages']}, "
              f"заказов: {st['seen_orders']}")
        print(f"Последняя проверка: {st['last_check'] or 'не было'}")
        return 0
    rep = check(inbox_path=inbox, dry_run=dry_run)
    print(f"FunPay {SHOP_NICK}: лотов на странице {len(rep['lots'])}")
    if rep.get("lots_note"):
        print(f"Лоты: {rep['lots_note']} ({len(rep['lots_new'])} шт.)")
    else:
        if rep["lots_new"]:
            print(f"Новые лоты: {', '.join(rep['lots_new'])}")
        if rep["lots_gone"]:
            print(f"Снятые лоты: {', '.join(rep['lots_gone'])}")
    if rep["error"]:
        print(f"ОШИБКА: {rep['error']}")
    if not rep["inbox"]:
        print("Снимок входящих отсутствует — сообщения не проверялись "
              "(снимок пишет браузерный вотч в "
              "~/workspace/funpay-shop/inbox.json).")
    else:
        print(f"Снимок от {rep.get('inbox_fetched_at')}: новых сообщений "
              f"{rep['messages_new']}, новых заказов {rep['orders_new']}")
        for path in rep["drafts"]:
            print(f"Черновик: {path}")
        for esc in rep["escalate"]:
            print(f"ЭСКАЛАЦИЯ: {esc['buyer']} — {esc['class']} "
                  f"(без ответа, блокировка + жалоба)")
    if not rep["messages_new"] and not rep["orders_new"] and not rep["error"]:
        print("Нового нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
