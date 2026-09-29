# JARVIS

JARVIS is a personal assistant under development. The project aims to use one identity, memory, and task system across devices. The current code contains the **Phase 0 foundation** and an initial Phase 1 OpenAI provider adapter. Chat and remote access are planned for later increments.

## Setup

Python 3.11 or newer is required.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

## Environment variables

See [.env.example](.env.example). Copy it to `.env` if useful, but export the variables in your shell or service manager; the application reads the process environment and does not automatically load `.env`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `JARVIS_DB_PATH` | `data/jarvis.sqlite3` | SQLite file path; parent directories are created at startup. |
| `JARVIS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `JARVIS_LLM_PROVIDER` | `none` | Set to `openai` to enable the OpenAI adapter when the chat API is installed. |
| `OPENAI_API_KEY` | unset | Server-side key for the optional OpenAI provider. |
| `JARVIS_OPENAI_MODEL` | unset | Explicit model to use with the optional OpenAI provider. |

Install the optional provider with `pip install -e '.[openai]'`. Set `JARVIS_LLM_PROVIDER=openai`, `OPENAI_API_KEY`, and `JARVIS_OPENAI_MODEL` to enable it after the chat API is installed. Leave `JARVIS_LLM_PROVIDER=none` for health checks without an LLM. The adapter passes `store=False` to the OpenAI Responses API and keeps credentials on the server. This adapter PR does not yet expose a chat route.

## Run

```sh
.venv/bin/uvicorn backend.api.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/health/live` and `http://127.0.0.1:8000/health/ready`. The second endpoint checks SQLite. Keep the service bound to localhost during Phase 0; authentication and remote access are not implemented yet.

## Test

```sh
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/python -m compileall -q backend tests
```

## Project documents

- [Architecture](docs/architecture.md)
- [Memory](docs/memory.md)
- [Research](docs/research.md)
- [Tools](docs/tools.md)
- [Security](docs/security.md)
- [Development plan](docs/development.md)
