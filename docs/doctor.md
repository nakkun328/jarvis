# Configuration doctor

`python -m backend.doctor` shows, before you start `python -m backend.serve`, which features the current environment would turn on and what is still missing.

```sh
.venv/bin/python -m backend.doctor                 # text report
.venv/bin/python -m backend.doctor --json          # machine-readable
.venv/bin/python -m backend.doctor --host 0.0.0.0  # check the bind you plan to pass to serve
```

It is read-only: no network calls, no files written or created, and no value from the environment is printed. Credentials, models and paths appear only as `set` / `not set`, `exists` / `missing`, or a fixed reason code. It reads the environment through the same `Settings`, `check_bind`, vault and personality rules the server uses, so it follows real behavior.

## Statuses

- `OK`: the feature will be active.
- `OFF`: the feature is not configured (the default for most features); nothing to fix.
- `INCOMPLETE`: something is configured but cannot work, or the server would refuse to start.

The exit code is `0` when nothing is `INCOMPLETE`, otherwise `1`.

## Areas and reason codes

| Area | Checks |
| --- | --- |
| chat | `JARVIS_LLM_PROVIDER`; `gemini` needs `GEMINI_API_KEY` and `JARVIS_GEMINI_MODEL`, `openai` needs `OPENAI_API_KEY` and `JARVIS_OPENAI_MODEL`; the optional package must be installed |
| models | `JARVIS_MODEL_CHOICES` ([model-select.md](model-select.md)): `OFF` when unset; `INCOMPLETE` when the default chat provider is not ready or an entry's provider key/package is missing (`MODEL_CHOICE_UNAVAILABLE`); shows only counts |
| research | `JARVIS_RESEARCH_ENABLED`, `JARVIS_SEARCH_PROVIDER` with `JARVIS_SEARCH_API_KEY`, a ready chat provider |
| router | `JARVIS_ROUTER` is `off`, `rule` or `llm`; `llm` needs a ready chat provider |
| chat_research | a router other than `off` plus research ready |
| login | `JARVIS_AUTH_PASSPHRASE_HASH`; a non-loopback `--host` needs login and Secure cookies |
| db, vault, personality | `JARVIS_DB_PATH` (a missing database is created on first start), `JARVIS_MEMORY_VAULT_PATH`, `JARVIS_PERSONALITY_PATH` |

Codes include `NOT_CONFIGURED`, `API_KEY_MISSING`, `MODEL_MISSING`, `DEPENDENCY_MISSING`, `RESEARCH_DISABLED`, `SEARCH_PROVIDER_MISSING`, `CHAT_PROVIDER_MISSING`, `ROUTER_NEEDS_CHAT_PROVIDER`, `RESEARCH_NOT_READY`, `BIND_REQUIRES_AUTH`, `BIND_REQUIRES_SECURE_COOKIE`, `PATH_MISSING` and `PATH_INVALID`. A value the server would reject (for example an unknown `JARVIS_ROUTER`, an out-of-range `JARVIS_SEARCH_MONTHLY_LIMIT`, or a search provider without a key) is reported as a single `CONFIG_INVALID` line, because the server refuses to start; start `backend.serve` once to see which variable it names.
