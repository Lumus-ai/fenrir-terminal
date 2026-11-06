# Fenrir Terminal

Fenrir's own orchestra/terminal: a Lix-powered agent engine, watchdog, RAM guard and scheduler. Built by Fenrir for Shadow.

- `fenrir-terminal/` — the terminal itself: `fenrir` command, agent (`fenrir_agent.py`), status/RAM guard (`fenrir_term.py`), plan and changelog.
- `fenrir-orchestra/` — the engine: batch/council/serve (`fenrir_orchestra.py`), provider routes (`providers.json`), watchdog.

Keys are never stored in this repo. API access goes through the connector surrogate (`dynamic_credentials`), never through files, env or logs.

Status: v1.3 live. Next: v1.3.1 agent fix, watchdog v2, RAM guard v2, v1.4 scheduler.
