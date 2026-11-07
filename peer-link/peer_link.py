#!/usr/bin/env python3
"""peer_link.py — standalone-модуль прямой связи двух агентов (этап 1).

Паттерн взят из мультиплеера Оркестры (комнаты: код-приглашение,
append/feed, роли), но реализация отдельная: НЕ в Оркестре и пока НЕ
в терминале. Это один самодостаточный файл (Python stdlib only).

Транспорт — Telegram Bot API: единственный двусторонний канал, доступный
обеим песочницам исходящими HTTPS-запросами. Бот боту напрямую писать
не может, поэтому точка встречи — приватная Telegram-группа с обоими
ботами и Shadow.

Протокол (комната):
  - Комната = {room_code (код-приглашение, 16 hex-символов),
    own_name, peer_name, chat_id (группа)}.
  - Сообщение в группе: "/peer <первые-8-символов-кода> to:<имя> <текст>".
    Префикс-команда — чтобы доходило при включённом privacy mode ботов;
    код — проверка членства в комнате (как invite-код Оркестры).
  - Отправка (append) = sendMessage в группу.
  - Чтение (feed) = long-poll getUpdates, новые сообщения — в peer-inbox.json
    (дедуп по message_id).
  - Фильтр входящих (всё должно совпасть): chat_id == заданной группе;
    отправитель — бот (from.is_bot); текст начинается с
    "/peer <код> to:<моё-имя> ". Остальное игнорируется молча.

Токен бота (порядок):
  1. Env PEER_BOT_TOKEN.
  2. Поле token в peer-link-config.json.
  3. Только на стороне Фенрира: tg-хелпер из
     ~/workspace/custom-connectors/custom.telegram-bot/bin/telegram.py
     (tg.api_call) — токен @Postingmog_bot лежит в безопасном коннекторе,
     его значение НЕ читается и нигде не показывается. Новый агент
     использует пути 1-2 со своим токеном @Tuunelbot.

Безопасность: сообщения — ТОЛЬКО текст для чтения агентом, никакого
авто-выполнения команд. Никаких eval/exec/os.system/shell=True.
Токен и полный код комнаты никогда не попадают в логи и вывод
(кроме однократной печати кода при setup). Работа только внутри
своей папки (конфиг и inbox рядом с модулем).

Использование:
  python3 peer_link.py setup --chat-id <id> [--code <код>] [--name <имя>]
      [--peer <имя-пир>] [--token <токен>]
  python3 peer_link.py send <текст>
  python3 peer_link.py poll [--timeout N]
  python3 peer_link.py inbox [--all]
  python3 peer_link.py read <id>
  python3 peer_link.py status
  python3 peer_link.py listen
"""

import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = "peer-link-config.json"
INBOX_FILE = "peer-inbox.json"
STATE_FILE = "peer-state.json"

DEFAULT_TIMEOUT = 30
MAX_TEXT_CHARS = 4096  # лимит sendMessage Telegram
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
CODE_RE = re.compile(r"^[0-9a-f]{16}$")

# Путь к tg-хелперу Фенрира (только его сторона; на другой стороне
# импорта просто не будет — это нормально, там работают пути 1-2).
TG_HELPER_DIR = os.path.expanduser(
    "~/workspace/custom-connectors/custom.telegram-bot/bin")


class PeerLinkError(Exception):
    """Ошибка модуля (пользовательская, без трейсбека в CLI)."""


def _atomic_write_json(path, data):
    """Атомарная запись JSON (tmp + fsync + os.replace)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _one_line(text):
    """Схлопнуть текст в одну строку для печати (без перевода строк)."""
    return " ".join(str(text).split())


class PeerLink:
    """Комната прямой связи: конфиг, отправка, опрос, почтовый ящик."""

    def __init__(self, base_dir=None, api_base=None, token=None):
        self.base_dir = base_dir or os.environ.get(
            "PEER_LINK_DIR") or MODULE_DIR
        self.config_path = os.path.join(self.base_dir, CONFIG_FILE)
        self.inbox_path = os.path.join(self.base_dir, INBOX_FILE)
        self.state_path = os.path.join(self.base_dir, STATE_FILE)
        self._api_base_override = api_base
        self._token_override = token
        self._tg = None
        self._tg_tried = False

    # ---------- конфиг / состояние ----------

    def load_config(self):
        cfg = _load_json(self.config_path, None)
        return cfg if isinstance(cfg, dict) else None

    def save_config(self, cfg):
        _atomic_write_json(self.config_path, cfg)

    def _load_inbox(self):
        items = _load_json(self.inbox_path, [])
        return items if isinstance(items, list) else []

    def _save_inbox(self, items):
        _atomic_write_json(self.inbox_path, items)

    def _load_state(self):
        st = _load_json(self.state_path, {})
        return st if isinstance(st, dict) else {}

    def _save_state(self, state):
        _atomic_write_json(self.state_path, state)

    def require_config(self):
        cfg = self.load_config()
        if not cfg:
            raise PeerLinkError(
                "нет конфига: сначала python3 peer_link.py setup --chat-id <id>")
        for key in ("chat_id", "room_code", "own_name", "peer_name"):
            if key not in cfg:
                raise PeerLinkError(
                    "конфиг неполный (нет %s): повторите setup" % key)
        return cfg

    # ---------- токен / транспорт ----------

    def _load_tg_helper(self):
        """Путь 3: tg-хелпер Фенрира. Возвращает модуль или None."""
        if self._tg_tried:
            return self._tg
        self._tg_tried = True
        try:
            if TG_HELPER_DIR not in sys.path:
                sys.path.insert(0, TG_HELPER_DIR)
            import telegram as tg
            self._tg = tg
        except Exception:
            self._tg = None
        return self._tg

    def _resolve_transport(self):
        """(kind, token): kind = 'direct' | 'tg'. Токен наружу не выдаём."""
        token = self._token_override or os.environ.get("PEER_BOT_TOKEN")
        if token:
            return ("direct", token)
        cfg = self.load_config() or {}
        if cfg.get("token"):
            return ("direct", cfg["token"])
        if self._load_tg_helper() is not None:
            return ("tg", None)
        raise PeerLinkError(
            "нет токена бота: задайте PEER_BOT_TOKEN, поле token в "
            "конфиге или tg-хелпер (сторона Фенрира)")

    def _api_call(self, method, params):
        """Вызов Bot API. Возвращает result. Ошибки — PeerLinkError."""
        kind, token = self._resolve_transport()
        if kind == "tg":
            try:
                resp = self._tg.api_call(
                    method, params,
                    is_form=(method == "sendMessage"))
            except Exception as e:
                raise PeerLinkError("tg-хелпер: %s" % e)
            if not isinstance(resp, dict) or not resp.get("ok"):
                raise PeerLinkError("Bot API: %s" %
                                    _one_line(resp)[:200])
            return resp.get("result")
        # direct: POST JSON на api.telegram.org (или PEER_API_BASE в тестах)
        api_base = self._api_base_override or os.environ.get("PEER_API_BASE") \
            or ("https://api.telegram.org/bot" + token)
        url = api_base.rstrip("/") + "/" + method
        data = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "peer-link/1.0"})
        try:
            with self._urlopen(req, timeout=60) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                body = json.load(e)
                detail = body.get("description", "")
            except Exception:
                detail = "HTTP %s" % e.code
            raise PeerLinkError("Bot API: %s" % _one_line(detail)[:200])
        except Exception as e:
            raise PeerLinkError("сеть: %s" % e)
        if not isinstance(body, dict) or not body.get("ok"):
            raise PeerLinkError("Bot API: %s" %
                                _one_line(body)[:200])
        return body.get("result")

    def _urlopen(self, req, timeout=60):
        """Открыть URL. Localhost — без прокси (тесты), остальное — как в env."""
        host = urllib.parse.urlparse(req.full_url).hostname or ""
        if host in ("127.0.0.1", "localhost", "::1"):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))
        else:
            opener = urllib.request.build_opener()
        return opener.open(req, timeout=timeout)

    # ---------- протокол ----------

    def _wire_text(self, cfg, text):
        return "/peer %s to:%s %s" % (
            cfg["room_code"][:8], cfg["peer_name"], text)

    def _expected_prefix(self, cfg):
        return "/peer %s to:%s " % (cfg["room_code"][:8], cfg["own_name"])

    def _parse_incoming(self, cfg, msg):
        """Разобрать входящее сообщение. Вернуть dict или None (игнор)."""
        chat = msg.get("chat") or {}
        if chat.get("id") != cfg["chat_id"]:
            return None
        frm = msg.get("from") or {}
        if not frm.get("is_bot"):
            return None
        text = msg.get("text") or ""
        prefix = self._expected_prefix(cfg)
        if not text.startswith(prefix):
            return None
        body = text[len(prefix):]
        if not body.strip():
            return None
        return {
            "id": msg.get("message_id"),
            "from": cfg["peer_name"],
            "from_username": frm.get("username", ""),
            "to": cfg["own_name"],
            "text": body,
            "ts": msg.get("date", 0),
            "chat_id": cfg["chat_id"],
            "read": False,
        }

    # ---------- команды ----------

    def setup(self, chat_id, code=None, name="fenrir", peer="tuunel",
              token=None):
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            raise PeerLinkError("chat_id должен быть целым числом")
        if chat_id == 0:
            raise PeerLinkError("chat_id не может быть 0")
        if code is None:
            code = secrets.token_hex(8)
            generated = True
        else:
            code = str(code).lower()
            generated = False
        if not CODE_RE.match(code):
            raise PeerLinkError(
                "код — 16 hex-символов (secrets.token_hex(8))")
        if not NAME_RE.match(name or ""):
            raise PeerLinkError(
                "имя — одно слово латиницей (буквы, цифры, _ , -)")
        if not NAME_RE.match(peer or ""):
            raise PeerLinkError(
                "имя пира — одно слово латиницей (буквы, цифры, _ , -)")
        cfg = self.load_config() or {}
        cfg.update({
            "chat_id": chat_id,
            "room_code": code,
            "own_name": name,
            "peer_name": peer,
        })
        if token:
            cfg["token"] = token
        self.save_config(cfg)
        return cfg, generated

    def send(self, text):
        cfg = self.require_config()
        if not text or not str(text).strip():
            raise PeerLinkError("пустое сообщение")
        text = str(text)
        if len(text) > MAX_TEXT_CHARS:
            raise PeerLinkError(
                "сообщение длиннее %d символов" % MAX_TEXT_CHARS)
        res = self._api_call("sendMessage", {
            "chat_id": cfg["chat_id"],
            "text": self._wire_text(cfg, text),
        })
        if not isinstance(res, dict) or "message_id" not in res:
            raise PeerLinkError("неожиданный ответ sendMessage")
        return res["message_id"]

    def poll(self, timeout=DEFAULT_TIMEOUT):
        """Один long-poll. Возвращает список НОВЫХ сообщений."""
        cfg = self.require_config()
        state = self._load_state()
        params = {"timeout": int(timeout)}
        if state.get("last_update_id") is not None:
            params["offset"] = int(state["last_update_id"]) + 1
        updates = self._api_call("getUpdates", params) or []
        inbox = self._load_inbox()
        seen_ids = {m.get("id") for m in inbox}
        new_items = []
        max_update_id = state.get("last_update_id")
        for upd in updates:
            uid = upd.get("update_id")
            if isinstance(uid, int) and (
                    max_update_id is None or uid > max_update_id):
                max_update_id = uid
            item = self._parse_incoming(cfg, upd.get("message") or {})
            if item is None:
                continue
            if item["id"] in seen_ids:
                continue
            seen_ids.add(item["id"])
            inbox.append(item)
            new_items.append(item)
        if max_update_id is not None:
            state["last_update_id"] = max_update_id
            self._save_state(state)
        if new_items:
            self._save_inbox(inbox)
        return new_items

    def listen(self, timeout=DEFAULT_TIMEOUT):
        """Цикл poll подряд (для живого теста). Ctrl+C — выход."""
        try:
            while True:
                for item in self.poll(timeout=timeout):
                    print("[%s] %s: %s" % (
                        item["id"], item["from"],
                        _one_line(item["text"])))
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    def inbox_list(self, show_all=False):
        items = self._load_inbox()
        if not show_all:
            items = [m for m in items if not m.get("read")]
        return items

    def mark_read(self, message_id):
        inbox = self._load_inbox()
        for m in inbox:
            if str(m.get("id")) == str(message_id):
                m["read"] = True
                self._save_inbox(inbox)
                return m
        raise PeerLinkError("сообщение %s не найдено" % message_id)

    def status(self):
        cfg = self.load_config()
        inbox = self._load_inbox()
        state = self._load_state()
        if not cfg:
            return {"configured": False}
        return {
            "configured": True,
            "chat_id": cfg.get("chat_id"),
            "own_name": cfg.get("own_name"),
            "peer_name": cfg.get("peer_name"),
            "room_code_prefix": str(cfg.get("room_code", ""))[:4] + "…",
            "token_present": bool(
                self._token_override or os.environ.get("PEER_BOT_TOKEN")
                or cfg.get("token") or self._load_tg_helper() is not None),
            "inbox_total": len(inbox),
            "inbox_unread": sum(1 for m in inbox if not m.get("read")),
            "last_update_id": state.get("last_update_id"),
        }


def _print_item(item):
    print("[%s] %s %s: %s" % (
        item.get("id"), "прочитано" if item.get("read") else "новое",
        item.get("from"), _one_line(item.get("text", ""))))


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        prog="peer_link.py",
        description="Прямая связь двух агентов: комната поверх Telegram Bot API.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="создать/обновить конфиг комнаты")
    s.add_argument("--chat-id", required=True,
                   help="id приватной группы (отрицательное число)")
    s.add_argument("--code", default=None,
                   help="код-приглашение, 16 hex; если нет — генерируется")
    s.add_argument("--name", default="fenrir", help="своё имя (по умолч. fenrir)")
    s.add_argument("--peer", default="tuunel", help="имя пира (по умолч. tuunel)")
    s.add_argument("--token", default=None,
                   help="токен своего бота. Хранить в файле — только локально, "
                        "в репозиторий и в чат не отправлять; лучше env "
                        "PEER_BOT_TOKEN")

    s = sub.add_parser("send", help="отправить сообщение в комнату")
    s.add_argument("text", help="текст сообщения")

    s = sub.add_parser("poll", help="один long-poll getUpdates")
    s.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)

    s = sub.add_parser("inbox", help="показать почтовый ящик")
    s.add_argument("--all", action="store_true", help="включая прочитанные")

    s = sub.add_parser("read", help="пометить сообщение прочитанным")
    s.add_argument("id", help="id сообщения")

    sub.add_parser("status", help="статус комнаты (без токена и кода)")

    s = sub.add_parser("listen", help="цикл poll подряд (живой тест)")
    s.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)

    args = p.parse_args(argv)
    link = PeerLink()
    try:
        if args.cmd == "setup":
            cfg, generated = link.setup(
                args.chat_id, code=args.code, name=args.name,
                peer=args.peer, token=args.token)
            if args.token:
                print("ВНИМАНИЕ: токен записан в конфиг — хранить только "
                      "локально, не пушить и не пересылать.")
            if generated:
                print("Код комнаты (показывается ОДИН раз — передайте "
                      "его пиру через Shadow): %s" % cfg["room_code"])
            print("Комната настроена: chat_id=%s, имя=%s, пир=%s" % (
                cfg["chat_id"], cfg["own_name"], cfg["peer_name"]))
            return 0
        if args.cmd == "send":
            mid = link.send(args.text)
            print("Отправлено, message_id=%s" % mid)
            return 0
        if args.cmd == "poll":
            new_items = link.poll(timeout=args.timeout)
            for item in new_items:
                print("[%s] %s: %s" % (
                    item["id"], item["from"], _one_line(item["text"])))
            if not new_items:
                print("Новых сообщений нет.")
            return 0
        if args.cmd == "inbox":
            items = link.inbox_list(show_all=args.all)
            if not items:
                print("Ящик пуст." if args.all else "Непрочитанных нет.")
            for item in items:
                _print_item(item)
            return 0
        if args.cmd == "read":
            link.mark_read(args.id)
            print("Прочитано: %s" % args.id)
            return 0
        if args.cmd == "status":
            st = link.status()
            if not st.get("configured"):
                print("Не настроено: python3 peer_link.py setup --chat-id <id>")
                return 0
            print("Комната: chat_id=%s, я=%s, пир=%s, код=%s" % (
                st["chat_id"], st["own_name"], st["peer_name"],
                st["room_code_prefix"]))
            print("Токен: %s. Ящик: %d всего, %d непрочитанных. "
                  "last_update_id=%s" % (
                      "задан" if st["token_present"] else "НЕ задан",
                      st["inbox_total"], st["inbox_unread"],
                      st["last_update_id"]))
            return 0
        if args.cmd == "listen":
            return link.listen(timeout=args.timeout)
    except PeerLinkError as e:
        print("peer_link: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
