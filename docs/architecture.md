# Architecture

## Product direction

One JARVIS will serve multiple devices with one identity, personality, memory, task system, and context. The core backend is written in Python and avoids operating-system-specific behavior. Device adapters and a web client will connect through APIs in later phases.

## Implemented in Phase 0

- `backend.api`: FastAPI application factory, startup initialization, liveness and readiness checks.
- `backend.core`: environment configuration, logging, SQLite connection and schema bootstrap.
- `backend.providers`: a contract for complete and streamed LLM responses. There is no provider implementation yet.

The application factory accepts explicit settings so tests and future embedding can use isolated databases. SQLite is opened for each operation and connections are closed. Schema version 1 creates `schema_migrations`; later migrations must be explicit and preserve existing data.

## Planned boundaries

- Personality and conversation context sit above the provider interface.
- Memory separates user facts, project facts, conversation logs, work state, temporary state, and self memory.
- The agent orchestrator owns plan, execution, observation, verification, retries, and reporting.
- Tools encapsulate external operations and permission checks. Skills compose tools into reusable procedures.
- The device layer routes work to capable agents while the server owns shared state.

Phase 1 will add a basic chat flow and minimal web interface. The later phases in [development.md](development.md) keep storage and provider choices replaceable.
