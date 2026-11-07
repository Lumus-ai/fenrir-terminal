#!/usr/bin/env python3
"""Тесты защищённого health-эндпоинта (health_endpoint.py).

Сервер поднимается на эфемерном порту, секрет берётся из временного
конфига через env FENRIR_PEER_CONFIG. Значение тестового секрета в отчёты
и логи теста не попадает — проверяется только его отсутствие в ответах
и в логах сервера.
"""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import health_endpoint

TEST_SECRET = "test-secret-for-health-endpoint-only-0123456789abcdef"


def _req(url, secret=None, method="GET"):
    req = urllib.request.Request(url, method=method)
    if secret is not None:
        req.add_header("X-Fenrir-Peer-Secret", secret)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class HealthEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg_path = os.path.join(cls.tmp.name, "peer-config.json")
        with open(cls.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"secret": TEST_SECRET}, f)
        os.environ["FENRIR_PEER_CONFIG"] = cls.cfg_path
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), health_endpoint.Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()
        os.environ.pop("FENRIR_PEER_CONFIG", None)

    def test_401_without_header(self):
        code, body = _req(self.base + "/health")
        self.assertEqual(code, 401)
        self.assertEqual(json.loads(body),
                         {"ok": False, "error": "unauthorized"})

    def test_401_wrong_secret(self):
        code, body = _req(self.base + "/health", secret="wrong-secret")
        self.assertEqual(code, 401)
        self.assertEqual(json.loads(body),
                         {"ok": False, "error": "unauthorized"})

    def test_200_with_correct_secret(self):
        code, body = _req(self.base + "/health", secret=TEST_SECRET)
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["service"], "fenrir-health")
        self.assertIn("uptime_s", data)
        self.assertIn("ram_available_mb", data)
        self.assertIsInstance(data["ram_available_mb"], int)
        self.assertIn("serve", data)
        self.assertIsInstance(data["serve"], dict)
        self.assertIn("time", data)
        # Секрет не должен попасть в тело ответа.
        self.assertNotIn(TEST_SECRET, body)

    def test_404_other_paths(self):
        for path in ("/", "/run", "/peer/message", "/health/extra"):
            code, _ = _req(self.base + path, secret=TEST_SECRET)
            self.assertEqual(code, 404, path)

    def test_404_other_methods(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            code, _ = _req(self.base + "/health", secret=TEST_SECRET,
                           method=method)
            self.assertEqual(code, 404, method)

    def test_503_without_secret(self):
        # Конфиг без секрета -> 503.
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({}, f)
        try:
            code, body = _req(self.base + "/health", secret=TEST_SECRET)
            self.assertEqual(code, 503)
            self.assertEqual(json.loads(body),
                             {"ok": False, "error": "not configured"})
        finally:
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump({"secret": TEST_SECRET}, f)

    def test_secret_not_in_logs_or_responses(self):
        # Собираем stdout сервера и тела ответов: секрета там быть не должно.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _req(self.base + "/health")
            _req(self.base + "/health", secret="wrong-secret")
            code, body = _req(self.base + "/health", secret=TEST_SECRET)
            _req(self.base + "/nope", secret=TEST_SECRET)
        self.assertEqual(code, 200)
        self.assertNotIn(TEST_SECRET, buf.getvalue())
        self.assertNotIn(TEST_SECRET, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
