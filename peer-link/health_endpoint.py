#!/usr/bin/env python3
"""Защищённая проверка здоровья Фенрира — только GET /health, только для Tuunel.

Слушает 127.0.0.1:4098. Авторизация — заголовок X-Fenrir-Peer-Secret,
сравнение через hmac.compare_digest с секретом из peer-config.json терминала
(поле secret; путь можно переопределить env FENRIR_PEER_CONFIG, для тестов).

Секрет НИКОГДА не печатается, не логируется и не попадает в ответы.
Секрет в конфиге отсутствует -> 503. Секрет неверный/отсутствует -> 401.
Верный -> 200 с состоянием serve и RAM. Все остальные пути и методы -> 404.

Никакого проксирования /run и других путей, никакого выполнения команд,
никакого доступа к файлам. В логах — только метод/путь/статус.
"""

import hmac
import json
import os
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 4098
DEFAULT_PEER_CONFIG = os.path.expanduser(
    "~/workspace/fenrir-terminal/state/peer-config.json")
SERVE_HEALTH = "http://127.0.0.1:4097/health"


def _peer_config_path():
    return os.environ.get("FENRIR_PEER_CONFIG") or DEFAULT_PEER_CONFIG


def _load_secret():
    """Секрет пира из конфига терминала. None — пир не настроен."""
    try:
        with open(_peer_config_path(), encoding="utf-8") as f:
            cfg = json.load(f)
        s = cfg.get("secret") if isinstance(cfg, dict) else None
        return s if isinstance(s, str) and s else None
    except (OSError, ValueError):
        return None


def _ram_available_mb():
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


def _serve_health():
    try:
        with urllib.request.urlopen(SERVE_HEALTH, timeout=5) as resp:
            data = json.loads(resp.read() or b"{}")
            return data if isinstance(data, dict) else {"ok": False,
                                                       "error": "bad response"}
    except Exception as e:  # noqa: BLE001 - любой сбой serve честно в ответ
        return {"ok": False, "error": type(e).__name__}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # логируем сами, без заголовков

    def _log(self, code):
        # Только метод/путь/статус. Никаких заголовков авторизации.
        print(f"{self.command} {self.path} -> {code}", flush=True)

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self._log(code)

    def _not_found(self):
        self._json(404, {"ok": False, "error": "not found"})

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/health":
            self._not_found()
            return
        secret = _load_secret()
        if not secret:
            self._json(503, {"ok": False, "error": "not configured"})
            return
        got = self.headers.get("X-Fenrir-Peer-Secret") or ""
        if not hmac.compare_digest(got, secret):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        serve = _serve_health()
        uptime = serve.get("uptime_s") if isinstance(serve, dict) else None
        body = {
            "ok": True,
            "service": "fenrir-health",
            "uptime_s": uptime,
            "ram_available_mb": _ram_available_mb(),
            "serve": serve,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        self._json(200, body)

    def do_POST(self):
        self._not_found()

    def do_PUT(self):
        self._not_found()

    def do_DELETE(self):
        self._not_found()

    def do_PATCH(self):
        self._not_found()

    def do_OPTIONS(self):
        self._not_found()

    def do_HEAD(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self._log(404)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"fenrir-health on http://{HOST}:{PORT} (GET /health only)",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
