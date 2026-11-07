#!/usr/bin/env python3
"""Fenrir Terminal v1.3.1 — агент с циклом инструментов.

v1.3.1: detect_done — агент распознаёт «работа готова»: если файлы на
диске чистые (verify_workdir) и последние раунды вызовов не меняют
файлы (или повторяют одни и те же вызовы), цикл завершается с True
вместо кручения до max_steps. Никогда True, пока verify не проходит
или модель просит подтверждения. См. detect_done() ниже.

Почему Луна «не писала файлы»: сама модель файлы не пишет никогда —
она только ВОЗВРАЩАЕТ вызов инструмента (tool_calls). Файл на диск
пишет тот, кто этот вызов выполняет: оркестра или этот агент.
Доказано 06.11.2026: klinch7/gpt-6-luna при предложенном write_file
дважды вернула finish_reason=tool_calls с верными аргументами.

Инструменты: write_file / append_file / edit_file / read_file /
list_dir / search / run_tests — все только внутри workdir.
Маршрутизация: ТОЛЬКО Lix (приказ Shadow 06.11.2026 ~11:30 — Klinch
и Anymodel не использовать вообще). Код с инструментами — Lix
qwen3.8-max, текст — Lix qwen3.8-max; фолбэк при 403/429/5xx
на следующий живой Lix из цепочки. Мёртвые модели Lix (claude-opus-5.5,
claude-sonnet-5.5, gpt-6-astra — 403) в цепочках отсутствуют.
Верификация: после записи агент САМ проверяет файлы на диске
(HTML: начало/конец, единственность </body></html>, дубли id;
Python: py_compile) и дописывает, пока не станет чисто.

Уроки живьём 06.11.2026, зашиты в код:
- большой файл — только чанками append_file по ~1000-1500 символов;
- </body></html> — только в самом последнем куске;
- read_file возвращает ХВОСТ файла (последние 4000 символов);
- пустой ответ модели = провал, два пустых подряд — стоп;
- Cloudflare 1010 блокирует дефолтный User-Agent Python-urllib —
  везде стоит fenrir-agent/1.0, не убирать.

Использование:
  python3 fenrir_agent.py --workdir <папка> --task "<задача>"
  python3 fenrir_agent.py --file tasks.jsonl   # очередь задач
  python3 fenrir_agent.py --workdir <папка> --task "..." --kind text
"""
import argparse, hashlib, json, os, re, subprocess, sys, time, urllib.request

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
from dynamic_credentials import add_surrogate_to_request, read_json_response

UA = "fenrir-agent/1.0"
HOSTS = {
    "custom.lix": ("https://lix.autos/v1", ["lix.autos"]),
}
# Порядок важен: первый живой берётся, при 403/429/5xx — следующий.
# ТОЛЬКО Lix — приказ Shadow 06.11.2026 ~11:30 (Klinch/Anymodel запрещены).
CHAINS = {
    # Код с инструментами: только Lix.
    "code": [("custom.lix", "qwen3.8-max"),
             ("custom.lix", "deepseek-v4-pro"),
             ("custom.lix", "deepseek-v4.1-flash"),
             ("custom.lix", "glm-5.3-prime")],
    # Текст без инструментов: только Lix.
    "text": [("custom.lix", "qwen3.8-max"),
             ("custom.lix", "deepseek-v4.1-flash"),
             ("custom.lix", "kimi-k3")],
}

SYSTEM = (
    "Ты — агент Фенрир Терминала. Файлы ты создаёшь ТОЛЬКО вызовами "
    "инструментов, никогда текстом в ответе.\n"
    "Правила сборки больших файлов:\n"
    "1. Большой файл — только чанками append_file по 1000-1500 символов за вызов, один вызов за шаг.\n"
    "2. Никогда не пиши </body></html> (и вообще закрывающие теги документа) раньше времени — только в самом последнем куске.\n"
    "3. Перед дописыванием проверь хвост файла через read_file: он возвращает последние 4000 символов. Не дублируй уже написанные секции — сверяйся с хвостом.\n"
    "4. Один инструмент за шаг, если содержимое большое. Не объясняй план текстом вместо вызова инструмента.\n"
    "5. Когда файл готов — вызови read_file и убедись, что он полный, и только потом отвечай текстом.\n"
    "6. Когда работа готова — НЕ вызывай больше инструменты. Ответь коротким текстом «Готово, задача выполнена» и остановись. Не перепроверяй готовый файл повторными вызовами."
)

TOOLS = [
    {"type": "function", "function": {"name": "write_file", "description": "Write text to a file inside the workdir (overwrites)",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "append_file", "description": "Append text to the end of a file inside the workdir (creates it if missing). Use for building big files in small chunks (~1000-1500 chars) instead of rewriting the whole file",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Replace one exact fragment in a file. Fails unless old_text occurs exactly once in the file",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a text file inside the workdir. Files longer than 4000 chars: returns the LAST 4000 chars (the tail)",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "list_dir", "description": "List files in the workdir (recursive, one level of subdirs)",
     "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "search", "description": "Search for a regex pattern in text files under the workdir. Returns file:line matches (max 50)",
     "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "glob": {"type": "string", "description": "optional filename substring filter, e.g. .html or .py"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "run_tests", "description": "Compile all .py files in the workdir (py_compile) and run pytest -q if tests exist. Returns the output",
     "parameters": {"type": "object", "properties": {}, "required": []}}},
]

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused")

def safe_path(workdir, path):
    p = os.path.realpath(os.path.join(workdir, path))
    if not p.startswith(os.path.realpath(workdir) + os.sep) and p != os.path.realpath(workdir):
        raise ValueError(f"path outside workdir: {path}")
    return p

def _iter_files(workdir):
    for root, dirs, files in os.walk(workdir):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
        for f in sorted(files):
            if f.startswith("."):
                continue
            yield os.path.join(root, f)

def run_tool(workdir, call):
    name = call["function"]["name"]
    args = json.loads(call["function"]["arguments"] or "{}")
    if name == "write_file":
        p = safe_path(workdir, args["path"])
        os.makedirs(os.path.dirname(p) or workdir, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(args["content"])
        return f"written {args['path']} ({len(args['content'])} chars)"
    if name == "append_file":
        p = safe_path(workdir, args["path"])
        os.makedirs(os.path.dirname(p) or workdir, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(args["content"])
        return f"appended {args['path']} ({len(args['content'])} chars, total {os.path.getsize(p)})"
    if name == "edit_file":
        p = safe_path(workdir, args["path"])
        with open(p, encoding="utf-8") as f:
            content = f.read()
        n = content.count(args["old_text"])
        if n == 0:
            return f"error: old_text not found in {args['path']}, file unchanged"
        if n > 1:
            return f"error: old_text occurs {n} times in {args['path']}, ambiguous — file unchanged"
        with open(p, "w", encoding="utf-8") as f:
            f.write(content.replace(args["old_text"], args["new_text"]))
        return f"edited {args['path']} (1 replacement)"
    if name == "read_file":
        p = safe_path(workdir, args["path"])
        with open(p, encoding="utf-8") as f:
            content = f.read()
        if len(content) > 4000:
            return f"...(показаны последние 4000 из {len(content)} символов)\n" + content[-4000:]
        return content
    if name == "list_dir":
        out = []
        for p in _iter_files(workdir):
            out.append(os.path.relpath(p, workdir))
        return "\n".join(out) or "(empty)"
    if name == "search":
        try:
            rx = re.compile(args["pattern"])
        except re.error as e:
            return f"error: bad regex: {e}"
        glob = args.get("glob") or ""
        hits = []
        for p in _iter_files(workdir):
            if glob and glob not in os.path.basename(p):
                continue
            try:
                with open(p, encoding="utf-8", errors="strict") as f:
                    lines = f.readlines()
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    hits.append(f"{os.path.relpath(p, workdir)}:{i}: {line.rstrip()[:200]}")
                    if len(hits) >= 50:
                        return "\n".join(hits) + "\n...(обрезано, 50+ совпадений)"
        return "\n".join(hits) if hits else "(no matches)"
    if name == "run_tests":
        out = []
        py_files = [p for p in _iter_files(workdir) if p.endswith(".py")]
        if py_files:
            r = subprocess.run([sys.executable, "-m", "py_compile"] + py_files,
                               capture_output=True, text=True, timeout=120, cwd=workdir)
            out.append("py_compile: " + ("OK" if r.returncode == 0 else "FAIL\n" + (r.stderr or "")[:2000]))
        else:
            out.append("py_compile: no .py files")
        has_tests = any("test" in os.path.basename(p) for p in py_files) or os.path.exists(os.path.join(workdir, "pytest.ini"))
        if has_tests:
            try:
                r = subprocess.run([sys.executable, "-m", "pytest", "-q"],
                                   capture_output=True, text=True, timeout=120, cwd=workdir)
                out.append("pytest:\n" + (r.stdout + r.stderr)[-3000:])
            except FileNotFoundError:
                out.append("pytest: not installed")
        else:
            out.append("pytest: no tests found")
        return "\n".join(out)
    return f"unknown tool {name}"

def verify_workdir(workdir):
    """Локальная проверка файлов на диске. Возвращает список проблем (пусто = чисто)."""
    issues = []
    for p in _iter_files(workdir):
        rel = os.path.relpath(p, workdir)
        try:
            with open(p, encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        if p.endswith(".html"):
            if "<html" in content and not content.lstrip().lower().startswith("<!doctype html"):
                issues.append(f"{rel}: не начинается с <!DOCTYPE html>")
            if "</html>" in content:
                if content.count("</html>") != 1:
                    issues.append(f"{rel}: </html> встречается {content.count('</html>')} раз (должен 1)")
                if content.count("</body>") != 1:
                    issues.append(f"{rel}: </body> встречается {content.count('</body>')} раз (должен 1)")
                tail = content.rstrip()
                if not tail.endswith("</html>"):
                    issues.append(f"{rel}: после </html> есть лишний текст")
            else:
                issues.append(f"{rel}: нет закрывающего </html>")
            ids = re.findall(r'id="([^"]+)"', content)
            dup = sorted({i for i in ids if ids.count(i) > 1})
            if dup:
                issues.append(f"{rel}: дублирующиеся id: {', '.join(dup)}")
        if p.endswith(".py"):
            r = subprocess.run([sys.executable, "-m", "py_compile", p],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                issues.append(f"{rel}: не компилируется: {(r.stderr or '').strip().splitlines()[-1][:200]}")
    return issues

def _asks_confirmation(text):
    """Модель просит подтверждения или ввода — завершаться нельзя."""
    return bool(re.search(
        r"подтвердите|продолжить\?|хотите\s+ли|вы\s+уверены|могу\s+ли\s+я"
        r"|что\s+делать\s+дальше|как\s+поступить|подтверждаете",
        text or "", re.IGNORECASE))


def _workdir_fingerprint(workdir):
    """Состояние файлов (путь, размер, sha1) — для детекции отсутствия прогресса."""
    items = []
    for p in _iter_files(workdir):
        try:
            with open(p, "rb") as f:
                digest = hashlib.sha1(f.read()).hexdigest()[:12]
            items.append((os.path.relpath(p, workdir), os.path.getsize(p), digest))
        except OSError:
            continue
    return tuple(sorted(items))


def _call_signature(call):
    """Нормализованная сигнатура вызова инструмента для детектора зацикливания."""
    try:
        args = json.loads(call["function"]["arguments"] or "{}")
    except ValueError:
        args = {}
    return (call["function"]["name"],
            json.dumps(args, sort_keys=True, ensure_ascii=False))


def detect_done(text="", verify_ok=False, no_progress_rounds=0, recent_calls=None):
    """v1.3.1: эвристики завершения (шаг 1 из спеки, адаптированы под процедурный цикл).

    Возвращает (done, reason). Никогда True, пока verify не проходит
    или модель просит подтверждения. Сигналы (по приоритету):
    - модель просит подтверждение -> False;
    - verify не проходит -> False;
    - >=2 раундов вызовов подряд без изменения файлов -> True;
    - 3 одинаковых раунда вызовов подряд -> True;
    - явный маркер завершения в ответе -> True.
    Пустой ответ без других сигналов -> False.
    """
    if _asks_confirmation(text):
        return False, "модель просит подтверждение"
    if not verify_ok:
        return False, "verify не проходит"
    if no_progress_rounds >= 2:
        return True, f"файлы готовы, {no_progress_rounds} раунда без изменений"
    if recent_calls and len(recent_calls) >= 3:
        last3 = recent_calls[-3:]
        if last3[0] and len(set(last3)) == 1:
            return True, "зацикливание: 3 одинаковых раунда вызовов подряд"
    if re.search(r"\[FINAL_ANSWER\]|\[WORK_DONE\]|задача выполнена"
                 r"|работа завершена|готово", text or "", re.IGNORECASE):
        return True, "явный маркер завершения в ответе"
    return False, "нет сигнала завершения"


def chat_once(connector, model, messages, max_tokens=2000):
    base, hosts = HOSTS[connector]
    body = json.dumps({"model": model, "messages": messages, "tools": TOOLS,
                       "tool_choice": "auto", "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": UA},
                                 method="POST")
    add_surrogate_to_request(req, connector, allowed_hosts=hosts)
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=300) as resp:
        return read_json_response(resp)

def chat(chain, messages, max_tokens=2000):
    """Пробует цепочку моделей по порядку; при 403/429/5xx или обрыве — следующий живой."""
    last = None
    for connector, model in chain:
        for attempt in range(2):
            try:
                d = chat_once(connector, model, messages, max_tokens)
                print(f"model in use: {connector}/{model}")
                return d, connector, model
            except Exception as e:
                last = e
                print(f"chat {connector}/{model} attempt {attempt + 1} failed: {e}")
                time.sleep(10)
        print(f"fallback: {connector}/{model} недоступен, следующий в цепочке")
    raise last

def run_task(task, workdir, kind, chain, max_steps, log):
    workdir = os.path.expanduser(workdir)
    os.makedirs(workdir, exist_ok=True)
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": task}]
    wrote = False
    asked_verify = False
    empty_streak = 0
    used = None
    # v1.3.1 detect_done: состояние для детекции отсутствия прогресса.
    prev_fingerprint = None
    no_progress = 0
    recent_calls = []
    for step in range(1, max_steps + 1):
        try:
            d, used_c, used_m = chat(chain, messages)
            used = (used_c, used_m)
        except Exception as e:
            log(f"step {step}: все модели цепочки недоступны: {e}")
            return False, used
        ch = d["choices"][0]
        msg = ch["message"]
        log(f"step {step}: finish={ch.get('finish_reason')} model={d.get('model')}")
        calls = msg.get("tool_calls") or []
        if not calls:
            text = (msg.get("content") or "").strip()
            log("answer: " + text[:500])
            if not text:
                empty_streak += 1
                if empty_streak >= 2:
                    log("stop: two empty answers in a row")
                    return False, used
                messages.append({"role": "assistant", "content": ""})
                messages.append({"role": "user", "content": "Ответ пришёл пустым. Сделай следующий шаг меньшим: вызови ОДИН инструмент с коротким содержимым."})
                continue
            empty_streak = 0
            if not wrote:
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": "Файл ещё не создан. Не объясняй план — вызови инструмент write_file или append_file прямо сейчас."})
                log("nudge: no write yet")
                continue
            # v1.3.1 detect_done: модель просит подтверждение — не завершаем.
            if _asks_confirmation(text):
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": "Подтверждаю: работа готова. Ответь коротко «Готово, задача выполнена» без новых вызовов инструментов."})
                log("detect_done: модель просит подтверждение — не завершаем")
                continue
            # Локальная верификация — истина на диске, а не слова модели.
            issues = verify_workdir(workdir)
            if issues:
                if not asked_verify:
                    asked_verify = True
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": "Локальная проверка файлов нашла проблемы — исправь их инструментами (edit_file/append_file), не переписывая файлы целиком:\n" + "\n".join(issues)})
                    log("verify FAILED:\n" + "\n".join(issues))
                    continue
                log("verify FAILED повторно, сдаёмся:\n" + "\n".join(issues))
                return False, used
            log("verify OK: файлы на диске чистые")
            return True, used
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            try:
                result = run_tool(workdir, call)
                if call["function"]["name"] in ("write_file", "append_file", "edit_file"):
                    wrote = True
                log(f"  tool {call['function']['name']} -> {result}")
            except Exception as e:
                result = f"error: {e}"
                log(f"  tool {call['function']['name']} -> {result}")
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        # v1.3.1 detect_done: после выполнения инструментов — есть ли прогресс?
        fp = _workdir_fingerprint(workdir)
        if fp == prev_fingerprint:
            no_progress += 1
        else:
            no_progress = 0
            prev_fingerprint = fp
        recent_calls.append(tuple(_call_signature(c) for c in calls))
        done, reason = detect_done(
            verify_ok=wrote and not verify_workdir(workdir),
            no_progress_rounds=no_progress, recent_calls=recent_calls)
        if done:
            log(f"detect_done: {reason} — завершаем с True")
            return True, used
    log("max steps reached")
    # v1.3.1: лимит шагов, но работа на диске сделана — это успех, а не провал.
    if wrote and not verify_workdir(workdir):
        log("detect_done: лимит шагов, но файлы на диске чистые — работа сделана")
        return True, used
    return False, used

def build_chain(kind, connector, model):
    chain = list(CHAINS.get(kind, CHAINS["code"]))
    if connector and model:
        pair = (connector, model)
        chain = [pair] + [p for p in chain if p != pair]
    return chain

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--connector", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--kind", default="code", choices=["code", "text"])
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--task", default=None)
    ap.add_argument("--file", default=None, help="tasks.jsonl: {task, workdir, kind?, max_steps?}")
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--outdir", default=None, help="куда писать логи очереди (по умолчанию runs/agent-<ts>)")
    a = ap.parse_args()

    if a.file:
        tasks = []
        with open(os.path.expanduser(a.file), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    tasks.append(json.loads(line))
        outdir = a.outdir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs",
                                          f"agent-{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(outdir, exist_ok=True)
        results = []
        for i, t in enumerate(tasks, 1):
            task_log = os.path.join(outdir, f"task-{i}.log")
            lines = []
            def log(s, _lines=lines):
                print(s)
                _lines.append(s)
            kind = t.get("kind", a.kind)
            chain = build_chain(kind, t.get("connector", a.connector), t.get("model", a.model))
            ok, used = run_task(t["task"], t["workdir"], kind, chain,
                                int(t.get("max_steps", a.max_steps)), log)
            with open(task_log, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            results.append({"task": t["task"][:80], "workdir": t["workdir"], "ok": ok,
                            "model": f"{used[0]}/{used[1]}" if used else None, "log": task_log})
            print(f"task {i}/{len(tasks)}: {'OK' if ok else 'FAIL'}")
        with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump({"total": len(tasks), "ok": sum(1 for r in results if r["ok"]), "results": results},
                      f, ensure_ascii=False, indent=2)
        print(f"queue done: {sum(1 for r in results if r['ok'])}/{len(tasks)} ok, logs in {outdir}")
        return 0 if all(r["ok"] for r in results) else 1

    if not a.task or not a.workdir:
        ap.error("--task и --workdir обязательны (или --file для очереди)")
    chain = build_chain(a.kind, a.connector, a.model)
    ok, used = run_task(a.task, a.workdir, a.kind, chain, a.max_steps, print)
    print("done" if ok else "FAILED")
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
