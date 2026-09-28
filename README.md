# JARVIS

JARVIS is a personal assistant under development. The project aims to use one identity, memory, and task system across devices. The current code is **Phase 0 foundation**: a FastAPI service, environment configuration, SQLite bootstrap, health checks, and a vendor-neutral LLM provider contract. Chat and remote access are planned for later phases.

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
