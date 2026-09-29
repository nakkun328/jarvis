# Development plan

## Current state and Phase 0 order

The initial worktree had only a README placeholder and `.gitignore`, with no Python environment or application code. Phase 0 tasks and dependencies:

1. Package and environment metadata establish the supported Python version and dependencies.
2. Configuration and logging give the app a controlled startup path.
3. SQLite connection and schema bootstrap establish persistent infrastructure.
4. Provider request and response interfaces establish a vendor-neutral boundary.
5. FastAPI app and health checks use configuration and SQLite.
6. Integration and failure-path tests, lint, syntax checks, and CI verify the foundation.
7. Documentation records boundaries and setup.

The provider contract and documentation can be developed independently once package layout is set. SQLite and API startup are dependent. Shared initial interfaces make a single sequential implementation the lowest-conflict approach here. Future independent provider adapters or documentation work can be split across worktrees; check edit overlap, dependencies, integration order, and tests first.

## Next phases

1. **Basic JARVIS:** chat API, conversation context, personality, a configured LLM provider, streaming, and a minimal responsive web UI.
2. **Memory:** persistent conversations, typed memories, Obsidian integration, vector search, retrieval, writing, self memory, and consolidation.
3. **Research:** search provider, query planning, source reading and evaluation, cross checking, conflict detection, citations, research levels, and reusable findings.
4. **Agent and tools:** registry, skill system, permission model, orchestrator, task queue, execution and verification.
5. **Productivity:** calendar, assignments, projects, GitHub, files, development assistance, and notifications.
6. **Multiple devices:** device agents, registry, remote execution, synchronization, and Raspberry Pi node.
7. **Voice and home:** wake word, speech input and output, smart home.

## Phase 1 implementation order

The OpenAI adapter, chat service/API, and web UI were developed in separate branches from the Phase 0 baseline. The provider contract and HTTP/SSE event shapes were set before parallel work. They were then merged in a local integration worktree and tested together. The integration smoke test exercises static asset delivery, streaming, context reuse, and a regular follow-up response with a simulated provider. A real OpenAI request still needs configured credentials for a live smoke check.

Keep every phase runnable. Use feature branches or worktrees, tests, diff review, and PR review. Do not merge into `main` without explicit user authorization. Avoid broad rewrites outside the active phase.
