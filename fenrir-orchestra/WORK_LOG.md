# WORK_LOG — Fenrir-Orchestra

## 06.11.2026 ~04:10 — создание (задача Shadow: своя версия оркестры)

Что сделано:
- Новая директория `~/workspace/fenrir-orchestra/` (orchestra-app, orchestra_android, opencode_app не тронуты; NeiP4n не тронут).
- `fenrir_orchestra.py` — единый CLI на Python stdlib only: probe | run | council | serve | status.
  - Ключи только через суррогат `dynamic_credentials.add_surrogate_to_request`, редиректы запрещены (NoRedirect) — паттерн из `lix_chat.py` / `klinch_probe.py`.
  - max_tokens по умолчанию 2000 (лечение потери финалов на капе 6144).
  - Атомарные записи на диск (tmp + os.replace); пустой ответ = failure.
  - Cooldown мёртвых моделей 30 мин в `state/cooldown.json`.
  - Council: 3 параллельных воркера (coder/reviewer/skeptic) + 4-й вызов-синтез; при падении синтеза — сводка-заглушка, не пустота.
  - Serve: только 127.0.0.1:4097, /health, POST /run, /runs.
- `providers.json` — маршруты без ключей; цепочки: code → lix flash/kimi-k3/klinch gpt-6-astra/qwen3.8-max; review → glm-5.3-prime/deepseek-v4-pro/kimi-k3; text → qwen-flash/anymodel gpt-6.1-sol/klinch gpt-6-astra.
- `watchdog.sh` + cron-инструкция (systemd --user недоступен — нет bus).
- `README.md`, `.gitignore`, каталоги `state/`, `runs/`, `logs/`.

Как сделано: один файл stdlib, конфиг отдельно в JSON, проверка — `status`, `probe`, живой `serve` (старт/ответ/стоп, в фоне не оставлен).

Что планировалось: probe/run/council/serve/status, watchdog на cron каждые 5 мин (повесить cron — отдельным шагом, только по слову Shadow, т.к. это persistent-стейт).

## Проверки (результаты — ниже, после прогона)

## 06.11.2026 ~04:12 — Lix-first (дополнение Shadow)

- Lix — строгий приоритет №1 во всех ролях до дедлайна ключа **09.11.2026** (понедельник, 3 дня безлимита — «выжать максимум»).
- Цепочки перестроены: все живые Lix-модели раньше любого Klinch/Anymodel во всех ролях (code/review/text).
- В `providers.json` зафиксированы: дедлайн `2026-11-09T23:59:59+04:00`, живые Lix на 06.11 ~04:00 (deepseek-v4.1-flash, deepseek-v4-pro, glm-5.3-prime, kimi-k3, qwen3.8-max, qwen-flash), мёртвые 403 (claude-opus-5.5, claude-sonnet-5.5, gpt-6-astra).
- Probe: перепроверка мёртвых ротацией — один короткий вызов Lix за probe на одну мёртвую модель (не чаще раза в 30 мин, `state/lix_dead_recheck.json`); ожила — cooldown снимается. Лимит токенов не увеличен: по-прежнему 1 короткий вызов на провайдера.
- Баг из первого probe: Lix вернул 404 на `GET /v1/models` при живом chat — chat-проверка теперь не зависит от `/v1/models`.
- Probe печатает остаток времени до дедлайна и пишет его в health-JSON (`lix_deadline`, `lix_time_left`).
- README: раздел «Lix: приоритет №1 до 09.11.2026», таблица ролей обновлена.
- Исправлен баг probe: anymodel2 не попадал в text-цепочку → «no model to probe». Добавлен `anymodel2/gpt-6.1-sol` в text-цепочку после anymodel (Lix-first не нарушен). Проверено офлайн-вызовом pick_probe_model (без сетевых вызовов): все 4 провайдера получают модель; ротация Lix следующей возьмёт claude-sonnet-5.5.
- Верификация 06.11 ~04:13: `probe` (exit 0): Lix «3д 19ч до дедлайна 09.11.2026»; anymodel alive («2»); klinch dead HTTP 429 (дневной лимит?); lix — перепроверка мёртвой claude-opus-5.5 → снова 403, в cooldown. `status` exit 0. Секретов в файлах нет (grep-скан).
- НЕ проверено: живой `run`/`council` на Lix-моделях (не тратил токены сверх probe); `serve` в этой итерации не перезапускался (код не менялся в serve-части); следующий probe перепроверит claude-sonnet-5.5 (ротация), затем gpt-6-astra.

## 06.11.2026 ~04:15 — тариф Lix + фикс probe + batch (задача Shadow)
- Тариф (чек Shadow): безлимит до 09.11.2026, 260 RPM, 2 000 000 TPM, до 6 параллельных, объём не ограничен. Зафиксирован в providers.json (tariff_lix) и README (раздел «Как выжимать лимит»).
- БАГ probe исправлен: base_url уже .../v1, поэтому base_url + "/v1/models" давал /v1/v1/models → 404 и ложный models_ok=false у Lix. Теперь models_url(): .../v1 + "/models".
- Council: 3 → 6 воркеров (coder, reviewer, skeptic, architect, tester, simplifier), кап MAX_PARALLEL=6 везде. run_id теперь уникален при параллельных запусках (мс + pid + счётчик); cooldown под lock.
- Новая команда batch --file tasks.jsonl: пул ровно 6 воркеров без пауз, каждый результат сразу на диск, summary.json с токенами и фактическими TPM/RPM.

## Проверки 06.11.2026 ~04:18 (после фикса probe + batch)
- `probe` exit 0: Lix models_count=22 (было 0 из-за /v1/v1/models → 404) — баг исправлен; anymodel alive, anymodel2 alive, klinch dead HTTP 429, Lix probe — перепроверка мёртвой (403, ротация) — ожидаемо. Отчёт runs/health-2026-11-06.json.
- `status` exit 0: cooldown 3 модели (klinch/gpt-6-astra, lix/claude-opus-5.5, lix/claude-sonnet-5.5).
- `batch` 3 короткие задачи, exit 0: ok=3/3, все на Lix (qwen-flash ×2, qwen3.8-max ×1), 197 токенов за 9.2 с, факт TPM=1290.8, RPM=32.8 — низко, потому что тестовые задачи микроскопические; к 2M TPM приближаются только большие чанки ×6 воркеров непрерывно. Файлы: runs/20261106-001732-.../summary.json + task-N/answer.md + meta.json.
- Находка: task-3 (code, max-tokens 50) — deepseek-v4.1-flash и kimi-k3 вернули пустой ответ finish_reason=length: при малом лимите reasoning съедает бюджет. Фолбэк сработал — qwen3.8-max дал верный ответ. Вывод: для code-задач не жать max_tokens до 50, дефолт 2000 правильный.
- Serve в фоне не оставлен, секретов в файлах нет.
- НЕ проверено: живой council на 6 воркеров (не тратил токены сверх probe/batch); batch из >6 задач (очередь на 6 воркерах проверена только на 3 задачах).

## 07.11.2026 ~15:30 (+04) — watchdog v2 вшит в живой watchdog.sh (приоритет №2, приказ Shadow «вшивай в терминал»)

- Что изменено: только `fenrir-orchestra/watchdog.sh` (тот же файл, тот же вызов из cron без аргументов). База сохранена: проверка `/health` curl -m 5, запуск через `setsid nohup python3 -u fenrir_orchestra.py serve`, лог `logs/watchdog.log`.
- Новое в v2 (суть из спеки пачек 1035/1038, без systemd и без несуществующих файлов — `fenrir-guardian.sh`, `/usr/local/bin`, юниты не брались):
  - Экспоненциальный backoff: `min(30 * 2^(n-1), 600)` сек; если с прошлой попытки прошло меньше задержки — рестарт пропускается, в лог пишется «skip restart, retry in Xs».
  - Счётчик последовательных сбоев в `state/watchdog-failures` («count last_attempt_epoch»), переживает запуски скрипта; при health OK сбрасывается («ok (recovered after N failures)»).
  - Захват хвоста serve.log при падении: последние 100 строк в `logs/crashes/crash-YYYYMMDD_HHMMSS.log`, ротация — последние 10 дампов.
  - Алерт после 3 сбоев подряд: один раз за серию — строка в лог + маркер-файл `state/watchdog-alert`; при восстановлении маркер снимается («alert cleared»). sleep-watch.sh пока маркер не читает (его не трогал — вне задачи), файл виден внешним проверкам.
  - Graceful shutdown: перед рестартом зависший serve получает SIGTERM, ожидание до 10 сек, затем SIGKILL. PID — из `state/serve.pid` с проверкой cmdline, фолбэк — pgrep с якорем `^python3 -u fenrir_orchestra.py serve` (якорь добавлен после того, как тест поймал ложное срабатывание pgrep на оболочке, в чьей командной строке была подстрока).
  - После рестарта — перепроверка /health через 3 сек («serve back up» / «serve still down after restart»).
  - `--dry-run` (или `WATCHDOG_DRY_RUN=1`): только лог «would ...», без убийств, запусков и записи state. Для тестов: `WATCHDOG_DIR`, `WATCHDOG_HEALTH_URL`, `WATCHDOG_SERVE_CMD`, `WATCHDOG_SERVE_MATCH`, `WATCHDOG_BASE_SLEEP`, `WATCHDOG_MAX_SLEEP`, `WATCHDOG_GRACE_TIMEOUT`.
- Проверки (подставной serve `fake-serve.py` на 4099, тестовая копия окружения в /tmp/wdtest, живой serve на 4097 не трогался):
  - `sh -n` — OK.
  - Здоровый serve: «ok», state не создаётся.
  - Одиночное падение: рестарт, pid-файл, crash tail (ровно 100 строк, первая «serve.log line 52», последняя «serve.log LAST LINE»), «serve back up».
  - 3 падения подряд (рестарт намеренно сломан): прогон 2 — «backoff 60s, skip restart», алерта нет; прогон 3 — ALERT + маркер-файл; прогон 4 — дубля алерта нет, backoff 240s.
  - Backoff: fails=5 + попытка 10с назад — пропуск «backoff 600s» (кап); fails=5 + попытка 1000с назад — рестарт проходит.
  - Восстановление: «ok (recovered after 6 failures)», «alert cleared», счётчик и маркер удалены.
  - Graceful shutdown: serve с игнорируемым SIGTERM (health 503) — «stopping hung serve pid … (SIGTERM, grace 2s)», «did not stop in 2s — SIGKILL», старый pid мёртв, новый pid поднят, «serve still down after restart» (честно — режим 503 ещё стоял).
  - Dry-run: ничего не стартуется/не убивается, state не пишется; найдена и исправлена двойная приставка «dry-run: dry-run:» и ложное срабатывание pgrep на оболочке (якорь ^).
  - Ручной прогон нового скрипта в production: exit 0, «ok» в `logs/watchdog.log`.
- Живой serve не пострадал: health OK до (uptime 9782.9с, pid 1962) и после (uptime ~10008с, тот же pid 1962).
- НЕ проверено: реальное падение живого serve (не убивал намеренно); чтение маркера `state/watchdog-alert` в sleep-watch.sh — не вшито, sleep-watch не менялся.
- Пуш в Lumus-ai/fenrir-terminal: коммит ff5acfd4bdfb0b69b65f44f56d980eaf81ea0a16 (ветка main, родитель 975630b4 — v1.3.1).
