# Security baseline

Phase 1 exposes health checks, a local chat API, and a web client. By default the API has no authentication or authorization, so bind the server to `127.0.0.1`. For remote devices, enable the single-owner login layer and put a TLS-terminating proxy or tunnel in front; see [login and remote access](auth.md). `python -m backend.serve` refuses a non-loopback bind unless login is enabled. When the OpenAI provider is enabled, chat messages are sent to its API for generation.

- Keep credentials in environment variables or a suitable secret store; never commit `.env` or hard-coded secrets.
- Never return secrets to frontend code or include them in logs.
- Validate external inputs at API and tool boundaries.
- Give tools the smallest needed privileges and require confirmation for red-level operations.
- Command execution only goes through named, application-registered commands with a deny-by-default argument grammar, a scrubbed environment, and bounded time and output; see [tools-shell.md](tools-shell.md). Never register a general shell or interpreter.
- Record execution and verification without logging sensitive request bodies. Application logs are structured and redacted; see [logging](logging.md) for what is and is not recorded.
- Review new dependencies and protect database and vault files with appropriate local permissions.

When `JARVIS_MEMORY_VAULT_PATH` is set, matching approved note excerpts are included in requests to the configured LLM provider. Keep this opt-in disabled for vaults that must remain local, and treat editable note text as untrusted reference data.

On POSIX systems, JARVIS creates a new SQLite file with mode `0600` and a new data directory with mode `0700`. Existing database files and directories retain their current permissions; review those permissions when moving an older installation.

`JARVIS_PERSONALITY_PATH` names a local TOML file that can only select predefined personality levels; it cannot add prompt text, change tool permissions, or remove the honesty rules. The loader refuses symlinks, non-regular files, files over 4 KiB, and unknown keys, and stops startup on any error without echoing file contents. See [personality settings](personality.md).

When `JARVIS_RESEARCH_ENABLED=1` (with a search provider and a chat provider), `POST /api/research/sessions` is the one route that spends search credits and makes outbound requests: the question text goes to the search service (which may keep and use it), the server fetches public result pages, and the question and page excerpts go to the chat provider. It is off by default, allows one research at a time, is bounded by a local monthly query cap, refuses a cross-origin request even with login off (the Origin/Referer/Sec-Fetch-Site check, so another web page cannot trigger it through your browser), and answers with fixed error codes only. Without login, any local process can still call it; keep the service on loopback or enable login. See [research](research.md#requesting-a-research-off-by-default).

The `.gitignore` excludes `.env` and `.env.*` except `.env.example`. Before a PR, inspect the diff and tracked files for secrets and generated data.
