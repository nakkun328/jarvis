# Architecture

## Product direction

One JARVIS will serve multiple devices with one identity, personality, memory, task system, and context. The core backend is written in Python and avoids operating-system-specific behavior. Phase 1 adds a local web client; remote device adapters remain later work.

## Implemented in Phase 0

- `backend.api`: FastAPI application factory, startup initialization, liveness and readiness checks.
- `backend.core`: environment configuration, structured redacted logging ([details](logging.md)), SQLite connection and schema bootstrap.
- `backend.providers`: a contract for complete and streamed LLM responses, with an OpenAI adapter added as the first Phase 1 increment.

Phase 1 adds an OpenAI Responses API adapter behind that contract. The adapter uses explicit model configuration, passes `store=False`, checks that each response completed, and keeps the API key server-side. Streaming exposes text deltas and closes the SDK stream after use. The chat service combines a personality prompt with bounded process-local context and exposes regular and streamed HTTP endpoints. The web client uses the same origin as the API.

The application factory accepts explicit settings so tests and future embedding can use isolated databases. SQLite is opened for each operation and connections are closed. Schema version 1 creates `schema_migrations` only in an empty, unversioned database. An existing unversioned database containing tables or other objects is rejected to avoid claiming unrelated data. Initialization holds a write transaction while checking and updating the version. Later migrations must be explicit and preserve existing data.

## Planned boundaries

- Personality and process-local conversation context sit above the provider interface; durable memory follows in Phase 2.
- Memory separates user facts, project facts, conversation logs, work state, temporary state, and self memory.
- The agent orchestrator owns plan, execution, observation, verification, retries, and reporting.
- Tools encapsulate external operations and permission checks. Skills compose tools into reusable procedures. The tool contract, registry, and permission policy exist in `backend.tools` (see [tools.md](tools.md)); no real tools are implemented yet.
- The device layer routes work to capable agents while the server owns shared state.

Task state, a single-worker queue, progress, and result verification are described in [tasks.md](tasks.md); the queue is a library with no daemon or UI yet, and a read-only HTTP API with an SSE progress stream reports task state.

The later phases in [development.md](development.md) keep storage and provider choices replaceable.
