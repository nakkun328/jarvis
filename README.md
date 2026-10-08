# JARVIS

JARVIS is a personal assistant under development. The project aims to use one identity, memory, and task system across devices. Phase 1 provides local chat, bounded conversation context, streamed responses, an OpenAI adapter, and a responsive web client. Phase 2 adds durable reviewed memories in SQLite and editable Obsidian notes, with a derived local vector index. Remote access needs the single-owner login layer ([auth](docs/auth.md)).

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
| `JARVIS_AUTH_PASSPHRASE_HASH` | unset | Turns on the single-owner login. Create it with `python -m backend.auth.hash_password`. Required to bind a non-loopback address. See [auth](docs/auth.md). |
| `JARVIS_AUTH_SIGNING_KEY` | random per start | At least 32 characters; signs session cookies. If unset, a random key is used and sessions reset on restart. |
| `JARVIS_AUTH_SESSION_HOURS` | `168` | Absolute session lifetime in hours (1 to 8760). |
| `JARVIS_AUTH_COOKIE_SECURE` | `true` | Mark the session cookie Secure. `false` is for plain-HTTP loopback development only. |
| `JARVIS_TRUSTED_PROXY` | `false` | `1` honors `X-Forwarded-For` / `X-Forwarded-Host` from the single reverse proxy in front. |
| `JARVIS_SEARCH_PROVIDER` | `none` | `none` or `tavily`. Search is off by default and nothing calls it yet. See [search](docs/research-search.md#tavily-adapter-off-by-default). |
| `JARVIS_SEARCH_API_KEY` | unset | Required when the search provider is `tavily`. Set it only in your own shell; never commit it. |
| `JARVIS_SEARCH_MONTHLY_LIMIT` | `800` | Local monthly cap on recorded search queries (a proposal below the free plan's 1,000 credits). Not the vendor's credit balance. |

Install the optional provider with `.venv/bin/python -m pip install -e '.[openai]'`. Set `JARVIS_LLM_PROVIDER=openai`, `OPENAI_API_KEY`, and `JARVIS_OPENAI_MODEL` to enable live chat. Leave `JARVIS_LLM_PROVIDER=none` to browse the UI and use health checks without an LLM; chat requests then return 503. The adapter passes `store=False` to the OpenAI Responses API and keeps credentials on the server.

For local vector indexing, install `.venv/bin/python -m pip install -e '.[vector]'` and use `backend.memory.chroma.ChromaVectorIndex`. It accepts caller-generated embeddings and is not yet connected to the chat API. See [vector search](docs/vector-search.md).

Set `JARVIS_MEMORY_VAULT_PATH` to an existing Obsidian vault to include matching approved notes in chat. The configured LLM provider receives bounded excerpts; see [chat memory behavior](docs/chat.md). With this variable unset, chat does not read or send long-term memory.

## Run

```sh
.venv/bin/python -m backend.serve --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` for the chat UI. The `/health/live` and `/health/ready` endpoints report service and SQLite status. Without login, keep the service on loopback: `backend.serve` refuses any other `--host` unless `JARVIS_AUTH_PASSPHRASE_HASH` is set. JARVIS does not provide TLS; for remote use, keep it on `127.0.0.1` behind a reverse proxy or tunnel that terminates HTTPS, as described in [auth](docs/auth.md). Starting `uvicorn backend.api.app:app` directly still works for loopback, but it does not perform that safety check. Successful conversation turns persist in SQLite, while active request locks remain process-local; run one worker for now.

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
- [Web shell, Memory screen and PWA](docs/web-shell.md)
- [Research](docs/research.md)
- [Quick Research](docs/research-quick.md)
- [Standard Research and follow-up queries](docs/research-standard.md)
- [Tools](docs/tools.md)
- [Read-only filesystem tools](docs/tools-filesystem.md)
- [Login and remote access](docs/auth.md)
- [Security](docs/security.md)
- [Development plan](docs/development.md)
