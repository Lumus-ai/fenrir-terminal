#!/usr/bin/env python3
"""Тесты peer_link.py: фейковый Bot API на 127.0.0.1, без живой Telegram."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from peer_link import PeerLink, PeerLinkError, CODE_RE  # noqa: E402

CHAT_ID = -1001234567890
CODE = "a1b2c3d4e5f60718"
OTHER_CODE = "0011223344556677"


class FakeBotAPI:
    """Состояние фейкового сервера: отправленные сообщения и updates."""

    def __init__(self):
        self.lock = threading.Lock()
        self.updates = []          # [{"update_id": int, "message": {...}}]
        self.next_update_id = 1
        self.next_message_id = 1
        self.sent_texts = []       # сырые тексты sendMessage (для проверки формата)

    def _new_update(self, message):
        with self.lock:
            upd = {"update_id": self.next_update_id,
                   "message": message}
            self.next_update_id += 1
            self.updates.append(upd)
            return upd

    def send_message(self, body):
        with self.lock:
            mid = self.next_message_id
            self.next_message_id += 1
        msg = {
            "message_id": mid,
            "chat": {"id": body.get("chat_id"), "type": "group"},
            "from": {"id": 999001, "is_bot": True,
                     "username": "FakeBot"},
            "text": body.get("text", ""),
            "date": 1700000000,
        }
        self.sent_texts.append(body.get("text", ""))
        self._new_update(msg)
        return {"ok": True, "result": msg}

    def get_updates(self, body):
        offset = body.get("offset")
        with self.lock:
            items = list(self.updates)
        if isinstance(offset, int):
            items = [u for u in items if u["update_id"] >= offset]
        return {"ok": True, "result": items}

    def inject_update(self, message):
        """Вставить сырой update (для тестов фильтра: человек, чужой чат)."""
        return self._new_update(message)


def make_handler(api):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _read_body(self):
            if self.command == "GET":
                qs = parse_qs(urlparse(self.path).query)
                return {k: v[0] for k, v in qs.items()}
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def _reply(self, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch()

        def _dispatch(self):
            method = urlparse(self.path).path.strip("/")
            body = self._read_body()
            if method == "sendMessage":
                self._reply(api.send_message(body))
            elif method == "getUpdates":
                # offset из query приходит строкой — привести к int
                if isinstance(body.get("offset"), str):
                    try:
                        body["offset"] = int(body["offset"])
                    except ValueError:
                        pass
                self._reply(api.get_updates(body))
            else:
                self._reply({"ok": False, "description": "unknown method"})
    return Handler


class PeerLinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = FakeBotAPI()
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(cls.api))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api.updates.clear()
        self.api.sent_texts.clear()
        self.api.next_update_id = 1
        self.api.next_message_id = 1
        # окружение для CLI-тестов: токен и API — только фейковые
        self._env = dict(os.environ)
        os.environ["PEER_BOT_TOKEN"] = "dummy-token"
        os.environ["PEER_API_BASE"] = self.base
        os.environ["PEER_LINK_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _link(self, name, peer, code=CODE, dirname=None):
        d = dirname or tempfile.mkdtemp(dir=self.tmp.name)
        link = PeerLink(base_dir=d, api_base=self.base, token="dummy")
        link.setup(CHAT_ID, code=code, name=name, peer=peer)
        return link

    # --- round-trip ---

    def test_round_trip(self):
        a = self._link("fenrir", "tuunel")
        b = self._link("tuunel", "fenrir")
        mid = a.send("привет из терминала")
        self.assertIsInstance(mid, int)
        got_b = b.poll(timeout=0)
        self.assertEqual(len(got_b), 1)
        self.assertEqual(got_b[0]["text"], "привет из терминала")
        self.assertEqual(got_b[0]["from"], "fenrir")
        mid2 = b.send("ответ принят")
        got_a = a.poll(timeout=0)
        self.assertEqual(len(got_a), 1)
        self.assertEqual(got_a[0]["text"], "ответ принят")

    def test_wire_format(self):
        a = self._link("fenrir", "tuunel")
        a.send("hello")
        self.assertEqual(len(self.api.sent_texts), 1)
        self.assertTrue(
            self.api.sent_texts[0].startswith("/peer %s to:tuunel hello"
                                              % CODE[:8]))

    # --- фильтры ---

    def test_wrong_code_ignored(self):
        a = self._link("fenrir", "tuunel", code=CODE)
        c = self._link("fenrir", "tuunel", code=OTHER_CODE)
        c.send("чужой код")
        got = a.poll(timeout=0)
        self.assertEqual(got, [])
        self.assertEqual(a.inbox_list(show_all=True), [])

    def test_wrong_addressee_ignored(self):
        a = self._link("fenrir", "tuunel")
        other = self._link("other", "tuunel")
        a.send("не тебе")
        self.assertEqual(other.poll(timeout=0), [])
        self.assertEqual(other.inbox_list(show_all=True), [])

    def test_human_sender_ignored(self):
        a = self._link("fenrir", "tuunel")
        self.api.inject_update({
            "message_id": 500,
            "chat": {"id": CHAT_ID, "type": "group"},
            "from": {"id": 123, "is_bot": False, "username": "shadow"},
            "text": "/peer %s to:fenrir от человека" % CODE[:8],
            "date": 1700000001,
        })
        self.assertEqual(a.poll(timeout=0), [])
        self.assertEqual(a.inbox_list(show_all=True), [])

    def test_wrong_chat_ignored(self):
        a = self._link("fenrir", "tuunel")
        self.api.inject_update({
            "message_id": 501,
            "chat": {"id": -999, "type": "group"},
            "from": {"id": 999001, "is_bot": True, "username": "FakeBot"},
            "text": "/peer %s to:fenrir чужой чат" % CODE[:8],
            "date": 1700000002,
        })
        self.assertEqual(a.poll(timeout=0), [])

    def test_dedup(self):
        a = self._link("fenrir", "tuunel")
        b = self._link("tuunel", "fenrir")
        a.send("один раз")
        self.assertEqual(len(b.poll(timeout=0)), 1)
        self.assertEqual(b.poll(timeout=0), [])
        self.assertEqual(len(b.inbox_list(show_all=True)), 1)

    # --- setup / валидация ---

    def test_setup_validation(self):
        link = PeerLink(base_dir=self.tmp.name, api_base=self.base,
                        token="dummy")
        for bad_chat in (0, "abc", None):
            with self.assertRaises(PeerLinkError):
                link.setup(bad_chat)
        for bad_code in ("zzz", "abcd", "A1B2C3D4E5F6071G", "a1b2c3d4e5f6071"):
            with self.assertRaises(PeerLinkError):
                link.setup(CHAT_ID, code=bad_code)
        for bad_name in ("two words", "Кириллица", "", "a b"):
            with self.assertRaises(PeerLinkError):
                link.setup(CHAT_ID, name=bad_name)
        with self.assertRaises(PeerLinkError):
            link.setup(CHAT_ID, peer="two words")

    def test_setup_generates_code(self):
        d = tempfile.mkdtemp(dir=self.tmp.name)
        link = PeerLink(base_dir=d, api_base=self.base, token="dummy")
        cfg, generated = link.setup(CHAT_ID)
        self.assertTrue(generated)
        self.assertTrue(CODE_RE.match(cfg["room_code"]))
        self.assertEqual(link.load_config()["room_code"], cfg["room_code"])

    def test_send_without_config(self):
        d = tempfile.mkdtemp(dir=self.tmp.name)
        link = PeerLink(base_dir=d, api_base=self.base, token="dummy")
        with self.assertRaises(PeerLinkError):
            link.send("hello")

    def test_empty_send_rejected(self):
        a = self._link("fenrir", "tuunel")
        with self.assertRaises(PeerLinkError):
            a.send("   ")

    # --- ящик и статус ---

    def test_read_marks_read(self):
        a = self._link("fenrir", "tuunel")
        b = self._link("tuunel", "fenrir")
        a.send("прочти меня")
        items = b.poll(timeout=0)
        mid = items[0]["id"]
        self.assertEqual(len(b.inbox_list()), 1)
        b.mark_read(mid)
        self.assertEqual(b.inbox_list(), [])
        all_items = b.inbox_list(show_all=True)
        self.assertEqual(len(all_items), 1)
        self.assertTrue(all_items[0]["read"])
        with self.assertRaises(PeerLinkError):
            b.mark_read(999999)

    def test_status_hides_secrets(self):
        a = self._link("fenrir", "tuunel")
        st = a.status()
        blob = json.dumps(st, ensure_ascii=False)
        self.assertNotIn("dummy", blob)
        self.assertNotIn(CODE, blob)
        self.assertIn(CODE[:4], blob)
        self.assertEqual(st["chat_id"], CHAT_ID)
        self.assertTrue(st["token_present"])

    def test_status_unconfigured(self):
        d = tempfile.mkdtemp(dir=self.tmp.name)
        link = PeerLink(base_dir=d, api_base=self.base, token="dummy")
        self.assertFalse(link.status()["configured"])

    # --- CLI (через main, env уже выставлен в setUp) ---

    def test_cli_setup_and_send(self):
        from peer_link import main
        rc = main(["setup", "--chat-id", str(CHAT_ID),
                   "--code", CODE, "--name", "fenrir", "--peer", "tuunel"])
        self.assertEqual(rc, 0)
        cfg = PeerLink(base_dir=self.tmp.name).load_config()
        self.assertEqual(cfg["room_code"], CODE)
        rc = main(["send", "cli hello"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.api.sent_texts[0].endswith("cli hello"))

    def test_cli_send_without_config_fails(self):
        from peer_link import main
        d = tempfile.mkdtemp(dir=self.tmp.name)
        os.environ["PEER_LINK_DIR"] = d
        rc = main(["send", "hello"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
