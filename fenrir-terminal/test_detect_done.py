#!/usr/bin/env python3
"""Юнит-тесты detect_done (v1.3.1) — эвристики завершения из шага 1 спеки.

Запуск: python3 test_detect_done.py
Сценарии: 1) успешное завершение, 2) запрос подтверждения -> False,
3) зацикливание -> True; плюс verify, отсутствие прогресса, пустой ответ.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fenrir_agent import (_asks_confirmation, _call_signature,
                          _workdir_fingerprint, detect_done)

PASSED = 0


def check(name, got, want):
    global PASSED
    assert got == want, f"{name}: got {got!r}, want {want!r}"
    PASSED += 1
    print(f"ok: {name}")


# --- Сценарий 1: успешное завершение (явный маркер + verify OK) ---
done, reason = detect_done(text="[FINAL_ANSWER] Задача выполнена.",
                           verify_ok=True)
check("1. маркер [FINAL_ANSWER] + verify OK -> True", done, True)
done, _ = detect_done(text="Готово, задача выполнена.", verify_ok=True)
check("1b. текст 'Готово, задача выполнена' + verify OK -> True", done, True)

# --- Сценарий 2: запрос подтверждения -> False, даже при verify OK ---
done, reason = detect_done(text="Нашёл 3 файла. Подтвердите удаление?",
                           verify_ok=True)
check("2. запрос подтверждения -> False", done, False)
check("2b. причина — подтверждение", "подтверждение" in reason, True)
done, _ = detect_done(text="Всё готово. Продолжить?", verify_ok=True)
check("2c. 'Продолжить?' + маркер 'готово' -> False", done, False)
check("2d. _asks_confirmation('Подтвердите?')", _asks_confirmation("Подтвердите?"), True)
check("2e. _asks_confirmation('Готово.')", _asks_confirmation("Готово."), False)

# --- Сценарий 3: зацикливание (3 одинаковых раунда вызовов) -> True ---
round_sig = (("read_file", '{"path": "index.html"}'),)
done, reason = detect_done(verify_ok=True,
                           recent_calls=[round_sig, round_sig, round_sig])
check("3. 3 одинаковых раунда + verify OK -> True", done, True)
check("3b. причина — зацикливание", "зацикливание" in reason, True)
done, _ = detect_done(verify_ok=True, recent_calls=[round_sig, round_sig])
check("3c. только 2 повтора -> False", done, False)
other = (("append_file", '{"path": "index.html"}'),)
done, _ = detect_done(verify_ok=True,
                      recent_calls=[round_sig, other, round_sig])
check("3d. 3 раунда, но не одинаковых -> False", done, False)

# --- verify не проходит -> всегда False ---
done, _ = detect_done(text="[FINAL_ANSWER] Задача выполнена.",
                      verify_ok=False)
check("4. маркер, но verify FAIL -> False", done, False)
done, _ = detect_done(verify_ok=False, no_progress_rounds=5,
                      recent_calls=[round_sig, round_sig, round_sig])
check("4b. зацикливание, но verify FAIL -> False", done, False)

# --- Отсутствие прогресса: >=2 раундов без изменения файлов ---
done, reason = detect_done(verify_ok=True, no_progress_rounds=2)
check("5. 2 раунда без изменений + verify OK -> True", done, True)
check("5b. причина — без изменений", "без изменений" in reason, True)
done, _ = detect_done(verify_ok=True, no_progress_rounds=1)
check("5c. 1 раунд без изменений -> False", done, False)
done, _ = detect_done(verify_ok=True, no_progress_rounds=2)
check("5d. 2 раунда, но verify FAIL -> False",
      detect_done(verify_ok=False, no_progress_rounds=2)[0], False)

# --- Пустой ответ без других сигналов -> False ---
done, _ = detect_done(text="", verify_ok=True)
check("6. пустой ответ -> False", done, False)
done, _ = detect_done(text="   ", verify_ok=True)
check("6b. пробелы вместо ответа -> False", done, False)

# --- _workdir_fingerprint: стабилен, меняется при правке ---
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "index.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write("<!DOCTYPE html><html></html>")
    fp1 = _workdir_fingerprint(d)
    fp2 = _workdir_fingerprint(d)
    check("7. fingerprint стабилен", fp1 == fp2, True)
    with open(p, "a", encoding="utf-8") as f:
        f.write("<!-- x -->")
    check("7b. fingerprint меняется при правке",
          _workdir_fingerprint(d) != fp1, True)

# --- _call_signature: одинаковые вызовы -> одинаковые сигнатуры ---
c1 = {"function": {"name": "read_file", "arguments": '{"path": "a.html"}'}}
c2 = {"function": {"name": "read_file", "arguments": '{"path": "a.html"}'}}
c3 = {"function": {"name": "read_file", "arguments": '{"path": "b.html"}'}}
check("8. одинаковые вызовы -> одинаковая сигнатура",
      _call_signature(c1) == _call_signature(c2), True)
check("8b. разные аргументы -> разная сигнатура",
      _call_signature(c1) != _call_signature(c3), True)

print(f"\nВсе тесты detect_done прошли: {PASSED} проверок OK.")
