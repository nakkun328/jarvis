# JARVIS

JARVIS is a personal assistant under development. The project aims to use one identity, memory, and task system across devices. The Phase 1 integration includes local chat, bounded conversation context, streamed responses, an OpenAI adapter, and a responsive web client. Persistent memory and remote access remain later phases.

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
| `JARVIS_LLM_PROVIDER` | `none` | Set to `openai` to enable the OpenAI adapter. |
| `OPENAI_API_KEY` | unset | Server-side key for the optional OpenAI provider. |
| `JARVIS_OPENAI_MODEL` | unset | Explicit model to use with the optional OpenAI provider. |

Install the optional provider with `.venv/bin/python -m pip install -e '.[openai]'`. Set `JARVIS_LLM_PROVIDER=openai`, `OPENAI_API_KEY`, and `JARVIS_OPENAI_MODEL` to enable live chat. Leave `JARVIS_LLM_PROVIDER=none` to browse the UI and use health checks without an LLM; chat requests then return 503. The adapter passes `store=False` to the OpenAI Responses API and keeps credentials on the server.

## Run

```sh
.venv/bin/uvicorn backend.api.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` for the chat UI. The `/health/live` and `/health/ready` endpoints report service and SQLite status. Keep the service bound to localhost; authentication and remote access are not implemented yet. Conversation context is held only in one process and disappears on restart, so run one worker for now.

## Test

```sh
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/python -m compileall -q backend tests
node --test frontend/test/chat-api.test.mjs
```

## Project documents

- [Architecture](docs/architecture.md)
- [Chat API and context](docs/chat.md)
- [Memory](docs/memory.md)
- [Research](docs/research.md)
- [Tools](docs/tools.md)
- [Security](docs/security.md)
- [Development plan](docs/development.md)
