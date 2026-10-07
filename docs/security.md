# Security baseline

Phase 1 exposes health checks, a local chat API, and a web client. The chat API has no authentication or authorization. Bind the server to `127.0.0.1`; add access control and a secure transport before allowing remote devices to connect. When the OpenAI provider is enabled, chat messages are sent to its API for generation.

- Keep credentials in environment variables or a suitable secret store; never commit `.env` or hard-coded secrets.
- Never return secrets to frontend code or include them in logs.
- Validate external inputs at API and tool boundaries.
- Give tools the smallest needed privileges and require confirmation for red-level operations.
- Record execution and verification without logging sensitive request bodies. Application logs are structured and redacted; see [logging](logging.md) for what is and is not recorded.
- Review new dependencies and protect database and vault files with appropriate local permissions.

When `JARVIS_MEMORY_VAULT_PATH` is set, matching approved note excerpts are included in requests to the configured LLM provider. Keep this opt-in disabled for vaults that must remain local, and treat editable note text as untrusted reference data.

On POSIX systems, JARVIS creates a new SQLite file with mode `0600` and a new data directory with mode `0700`. Existing database files and directories retain their current permissions; review those permissions when moving an older installation.

The `.gitignore` excludes `.env` and `.env.*` except `.env.example`. Before a PR, inspect the diff and tracked files for secrets and generated data.
