#!/usr/bin/env python3
"""Тесты пульта v3.2 (fenrir_remote.py). Запуск: python3 test_remote.py"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fenrir_remote as R


class RemoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="remote-test-")
        self.orig = {}
        for name in ("CONFIG_PATH", "PENDING_PATH", "RATELIMIT_PATH",
                     "AUDIT_LOG", "OFFSET_PATH"):
            self.orig[name] = getattr(R, name)
            setattr(R, name, os.path.join(self.tmp, name.lower()))
        self.worklog_calls = []
        self.orig_worklog = R.worklog
        R.worklog = lambda *a, **k: self.worklog_calls.append(a) or True
        self.exec_calls = []

    def tearDown(self):
        for name, val in self.orig.items():
            setattr(R, name, val)
        R.worklog = self.orig_worklog
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------
    def allow(self, uid=111):
        R.save_config({"chat_id": 999, "allowed_users": [uid],
                       "notifications": True})

    def stub_executor(self, command, args):
        self.exec_calls.append((command, args))
        return f"выполнено: {command}"

    def reset_rate(self):
        try:
            os.remove(R.RATELIMIT_PATH)
        except OSError:
            pass

    # -- парсер -------------------------------------------------------
    def test_parse_ok(self):
        self.assertEqual(R.parse_command("/status"), ("status", []))
        self.assertEqual(R.parse_command("tasks"), ("tasks", []))
        self.assertEqual(R.parse_command("/confirm abc123"),
                         ("confirm", ["abc123"]))
        self.assertEqual(R.parse_command("/STOP"), ("stop", []))

    def test_parse_unknown_and_empty(self):
        cmd, err = R.parse_command("/foobar")
        self.assertIsNone(cmd)
        self.assertIn("Неизвестная", err)
        cmd, err = R.parse_command("   ")
        self.assertIsNone(cmd)
        self.assertIn("Пустая", err)

    # -- whitelist ----------------------------------------------------
    def test_no_config_denies_everyone(self):
        reply = R.handle(111, "/status")
        self.assertIn("не настроен", reply)
        self.assertEqual(self.exec_calls, [])

    def test_unknown_user_denied(self):
        self.allow(111)
        reply = R.handle(222, "/status")
        self.assertIn("Нет доступа", reply)

    # -- rate limit ---------------------------------------------------
    def test_rate_limit_blocks_second_command(self):
        self.allow()
        R.handle(111, "/status")
        reply = R.handle(111, "/status")
        self.assertIn("Слишком часто", reply)

    def test_rate_limit_does_not_block_confirm(self):
        self.allow()
        R.handle(111, "/stop")
        # confirm сразу же — лимит не мешает завершить поток
        pending = R._load_pending()
        pid = next(iter(pending))
        reply = R.handle(111, f"/confirm {pid}",
                         executor=self.stub_executor)
        self.assertIn("Выполнено", reply)
        self.assertEqual(self.exec_calls, [("stop", [])])

    # -- подтверждения -------------------------------------------------
    def test_dangerous_creates_pending_no_exec(self):
        self.allow()
        reply = R.handle(111, "/stop", executor=self.stub_executor)
        self.assertIn("Подтвердите", reply)
        self.assertIn("/confirm", reply)
        self.assertEqual(self.exec_calls, [])
        pending = R._load_pending()
        self.assertEqual(len(pending), 1)
        req = next(iter(pending.values()))
        self.assertEqual(req["command"], "stop")
        self.assertEqual(req["user"], "111")

    def test_confirm_executes_once(self):
        self.allow()
        R.handle(111, "/restart", executor=self.stub_executor)
        pid = next(iter(R._load_pending()))
        reply = R.handle(111, f"/confirm {pid}", executor=self.stub_executor)
        self.assertIn("Выполнено", reply)
        self.assertEqual(self.exec_calls, [("restart", [])])
        self.assertEqual(R._load_pending(), {})
        # повторный confirm — отказ
        reply2 = R.handle(111, f"/confirm {pid}", executor=self.stub_executor)
        self.assertIn("Неизвестный id", reply2)
        self.assertEqual(len(self.exec_calls), 1)

    def test_confirm_wrong_user_refused(self):
        self.allow(111)
        R.save_config({"chat_id": 999, "allowed_users": [111, 222],
                       "notifications": True})
        R.handle(111, "/stop", executor=self.stub_executor)
        pid = next(iter(R._load_pending()))
        reply = R.handle(222, f"/confirm {pid}", executor=self.stub_executor)
        self.assertIn("не ваше", reply)
        self.assertEqual(self.exec_calls, [])

    def test_confirm_expired_refused(self):
        self.allow()
        R.handle(111, "/stop", executor=self.stub_executor)
        pid = next(iter(R._load_pending()))
        pending = R._load_pending()
        pending[pid]["expires"] = time.time() - 1
        R._save_pending(pending)
        reply = R.handle(111, f"/confirm {pid}", executor=self.stub_executor)
        self.assertIn("Неизвестный id", reply)
        self.assertEqual(self.exec_calls, [])

    def test_reject_cancels(self):
        self.allow()
        R.handle(111, "/start", executor=self.stub_executor)
        pid = next(iter(R._load_pending()))
        reply = R.handle(111, f"/reject {pid}", executor=self.stub_executor)
        self.assertIn("Отменено", reply)
        self.assertEqual(self.exec_calls, [])
        self.assertEqual(R._load_pending(), {})

    def test_confirm_without_args(self):
        self.allow()
        reply = R.handle(111, "/confirm")
        self.assertIn("Укажите id", reply)

    # -- команды чтения -----------------------------------------------
    def test_read_commands_work(self):
        self.allow()
        reply = R.handle(111, "/status")
        self.assertIn("Фенрир Терминал", reply)
        self.assertIn("RAM", reply)

    def test_tasks_lists_real_tasks(self):
        self.allow()
        reply = R.handle(111, "/tasks")
        self.assertIn("FNR-001", reply)

    def test_help_and_pending(self):
        self.allow()
        self.assertIn("/status", R.handle(111, "/help"))
        self.reset_rate()
        self.assertIn("нет", R.handle(111, "/pending"))

    # -- аудит ---------------------------------------------------------
    def test_audit_log_records(self):
        self.allow()
        R.handle(111, "/status")
        with open(R.AUDIT_LOG, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("user=111", content)
        self.assertIn("status", content)
        self.assertIn("ok", content)

    def test_history_shows_recent(self):
        self.allow()
        R.handle(111, "/status")
        self.reset_rate()
        reply = R.handle(111, "/history")
        self.assertIn("История команд", reply)
        self.assertIn("status", reply)

    # -- уведомления ----------------------------------------------------
    def test_notify_one_line_dry_run(self):
        self.allow()
        sent, line = R.notify("упало", "serve не отвечает", dry_run=True)
        self.assertFalse(sent)
        self.assertEqual(line.count("\n"), 0)
        self.assertIn("🐺 упало: serve не отвечает", line)
        self.assertTrue(line.startswith("[dry-run]"))

    def test_notify_no_chat_id_is_dry_run(self):
        R.save_config({"chat_id": None, "allowed_users": [111],
                       "notifications": True})
        sent, line = R.notify("готово", "задача выполнена")
        self.assertFalse(sent)
        self.assertIn("dry-run", line)

    def test_notify_unknown_event(self):
        sent, line = R.notify("что-то", "текст")
        self.assertFalse(sent)
        self.assertIn("Неизвестное событие", line)

    def test_notify_sends_via_sender(self):
        self.allow()
        sent_calls = []

        def fake_sender(chat_id, text):
            sent_calls.append((chat_id, text))
            return {"ok": True}

        sent, line = R.notify("готово", "задача FNR-001 выполнена",
                              sender=fake_sender)
        self.assertTrue(sent)
        self.assertEqual(sent_calls, [(999, "🐺 готово: задача FNR-001 выполнена")])
        self.assertEqual(line.count("\n"), 0)

    # -- listen без конфига -------------------------------------------
    def test_listen_refuses_without_config(self):
        self.assertEqual(R.listen(timeout=1), 0)

    # -- работа с реальным конфигом ------------------------------------
    def test_allow_and_config_roundtrip(self):
        self.assertTrue(R.add_allowed_user(555))
        self.assertFalse(R.add_allowed_user(555))
        cfg = R.load_config()
        self.assertEqual(cfg["allowed_users"], [555])
        self.assertIsNone(cfg["chat_id"])


if __name__ == "__main__":
    result = unittest.main(verbosity=1, exit=False)
    ok = result.result.wasSuccessful()
    n = result.result.testsRun
    failed = len(result.result.failures) + len(result.result.errors)
    print(f"\nИТОГО: {n - failed} пройдено, {failed} провалено")
    sys.exit(0 if ok else 1)
