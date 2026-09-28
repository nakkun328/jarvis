# Security baseline

Phase 0 exposes only health endpoints. Bind to `127.0.0.1`; authentication, authorization, and safe remote access must be built before exposing personal data or control APIs over a network.

- Keep credentials in environment variables or a suitable secret store; never commit `.env` or hard-coded secrets.
- Never return secrets to frontend code or include them in logs.
- Validate external inputs at API and tool boundaries.
- Give tools the smallest needed privileges and require confirmation for red-level operations.
- Record execution and verification without logging sensitive request bodies.
- Review new dependencies and protect database and vault files with appropriate local permissions.

The `.gitignore` excludes `.env` and `.env.*` except `.env.example`. Before a PR, inspect the diff and tracked files for secrets and generated data.
