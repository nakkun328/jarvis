# JARVIS

JARVIS is a personal assistant under development. The project aims to use one identity, memory, and task system across devices. Phase 1 provides local chat, bounded conversation context, streamed responses, an OpenAI adapter, and a responsive web client. Phase 2 adds durable reviewed memories in SQLite and editable Obsidian notes, with a derived local vector index. Remote access remains future work.

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
| `JARVIS_MEMORY_VAULT_PATH` | unset | Opt in to including matching approved vault notes in chat requests. The configured provider receives those excerpts. |
| `JARVIS_PERSONALITY_PATH` | unset | Optional TOML file that selects personality levels; unset uses the documented initial values. An invalid file stops startup. See [personality settings](docs/personality.md). |
| `JARVIS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. Logs are JSON lines on stderr; see [logging](docs/logging.md). |
| `JARVIS_LLM_PROVIDER` | `none` | Set to `openai` to enable the OpenAI adapter. |
| `OPENAI_API_KEY` | unset | Server-side key for the optional OpenAI provider. |
| `JARVIS_OPENAI_MODEL` | unset | Explicit model to use with the optional OpenAI provider. |

Install the optional provider with `.venv/bin/python -m pip install -e '.[openai]'`. Set `JARVIS_LLM_PROVIDER=openai`, `OPENAI_API_KEY`, and `JARVIS_OPENAI_MODEL` to enable live chat. Leave `JARVIS_LLM_PROVIDER=none` to browse the UI and use health checks without an LLM; chat requests then return 503. The adapter passes `store=False` to the OpenAI Responses API and keeps credentials on the server.

For local vector indexing, install `.venv/bin/python -m pip install -e '.[vector]'` and use `backend.memory.chroma.ChromaVectorIndex`. It accepts caller-generated embeddings and is not yet connected to the chat API. See [vector search](docs/vector-search.md).

Set `JARVIS_MEMORY_VAULT_PATH` to an existing Obsidian vault to include matching approved notes in chat. The configured LLM provider receives bounded excerpts; see [chat memory behavior](docs/chat.md). With this variable unset, chat does not read or send long-term memory.

## Run

```sh
.venv/bin/uvicorn backend.api.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` for the chat UI. The `/health/live` and `/health/ready` endpoints report service and SQLite status. Keep the service bound to localhost; authentication and remote access are not implemented yet. Successful conversation turns persist in SQLite, while active request locks remain process-local; run one worker for now.

## Test

```sh
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/python -m compileall -q backend tests
node --test frontend/test/*.test.mjs
```

## Project documents

- [Architecture](docs/architecture.md)
- [Chat API and context](docs/chat.md)
- [Personality settings](docs/personality.md)
- [Memory](docs/memory.md)
- [Vector index rebuild](docs/index-rebuild.md)
- [Local memory review](docs/memory-review.md)
- [Research](docs/research.md)
- [Tools](docs/tools.md)
- [Security](docs/security.md)
- [Development plan](docs/development.md)
