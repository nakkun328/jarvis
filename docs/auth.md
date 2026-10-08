# Login for a single owner (remote access)

JARVIS has one user: the owner. By default there is no login and the server must stay on loopback. When the owner wants to reach JARVIS from other places, turn on the login layer described here. **Without it JARVIS refuses to start on a non-loopback address.**

## What it does

- Setting `JARVIS_AUTH_PASSPHRASE_HASH` enables authentication. Unset (the default) leaves behavior exactly as before, with no `/login`, no `/api/auth/*`, and no extra headers.
- One passphrase, checked against a scrypt hash. A correct login sets a signed, expiring session cookie; there is no database table and no schema change.
- Every route except those listed below needs the cookie. API calls without it get `401 {"detail":"unauthorized"}`; page requests are redirected (303) to `/login`.

Public routes (and nothing else): `GET /login`, `GET /static/login.css`, `GET /static/login.js`, `POST /api/auth/login`, `GET /api/auth/status` (returns only `{"authenticated": true|false}`), `GET /health/live`. `/health/ready` is **not** public, so an external uptime monitor should use `/health/live`.

## Threat model: one owner, reachable from the internet

Protected:

- **Anonymous access.** Nobody without the session cookie can read or change conversations, memories, tasks or research, or use the LLM provider through JARVIS.
- **Passphrase guessing.** scrypt hashing (memory-hard); at most 5 attempts per client, then lockouts of 30 s, 60 s, 120 s ... up to 15 min; a global failure budget across all clients; a fixed delay after each failure; generic error messages. Only the direct peer address is used (see *Reverse proxy*); IPv6 clients are grouped by /64.
- **Cookie theft by scripts.** The cookie is `HttpOnly`, so page scripts cannot read it.
- **Cross-site request forgery.** `SameSite=Strict`, plus every POST/PUT/PATCH/DELETE must carry an `Origin` (or `Referer`) whose host matches the `Host` header. Requests with neither are refused, so scripts calling the API must send an `Origin` header themselves.
- **Cookie over plain HTTP.** The default cookie is `Secure` with the `__Host-` prefix, so a browser never sends it over HTTP and rejects attempts to set it from other subdomains.
- **Tampering and forgery.** Sessions are HMAC-SHA256 signed, verified in constant time, absolute-lifetime only (default 168 h; no sliding refresh). The MAC key also depends on the stored passphrase hash, so changing the passphrase logs every device out.
- **Leaks through logs.** Only fixed event names (`auth.login.success`, `auth.login.failure`, `auth.login.locked`, `auth.logout`) and a keyed, truncated client identifier are logged. Passphrases, hashes, tokens, keys and addresses are never logged.
- **Browser hardening.** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` (unless a route sets its own, such as event streams), and a strict Content-Security-Policy on the login page (no inline script).

Not protected, by design or limitation:

- **Transport.** JARVIS does not do TLS. Plain HTTP across the internet exposes the passphrase and the session. Terminate TLS in front (see below).
- **A compromised device or host.** Malware, a keylogger, or someone using an unlocked, signed-in device has full access. There is no second factor.
- **Phishing of the passphrase**, and a weak passphrase. Use a long unique passphrase (at least 12 characters is enforced; more is better).
- **Per-device revocation.** Sessions are stateless. Logout only clears the cookie in that browser; a copied cookie stays valid until it expires or the signing key changes.
- **Lockout as denial of service.** Someone who can reach the login endpoint can spend the global failure budget and delay *new* logins for up to 15 minutes. Existing sessions keep working. Restarting JARVIS resets the limiter, and loopback access is unaffected by the proxy.
- **The limiter is per process and in memory.** Run a single worker (as the rest of JARVIS already requires). With several workers each has its own limiter and an ephemeral key would differ between them.
- **Everything after login.** Once signed in, the owner session has the full API, including tools the application registers. Login does not reduce what an authenticated session can do.
- **Unauthenticated loopback mode.** With login off, any local process, and any web page that can reach `127.0.0.1` through DNS rebinding, can use the API. That mode is for a trusted single-user machine only.
- **Starting uvicorn directly** (`uvicorn ... --host 0.0.0.0`) bypasses the startup safety check. Use `python -m backend.serve`.

## Setup

1. Create the passphrase hash. The passphrase is read with a hidden prompt, never from arguments or the environment, and only the hash is printed.

   ```sh
   python -m backend.auth.hash_password
   ```

   The result looks like `scrypt$32768$8$3$...$...`. Passphrases are normalized (Unicode NFKC) before hashing, so full-width and half-width forms match.

2. Create a signing key (at least 32 characters):

   ```sh
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

3. Provide both to the server process as environment variables, using a service manager's secret store or a root-only file. The hash contains `$`, so quote it with single quotes in a shell, and escape or avoid shell expansion in `.env`-style files that expand variables. Never commit either value.

   | Variable | Default | Meaning |
   | --- | --- | --- |
   | `JARVIS_AUTH_PASSPHRASE_HASH` | unset | Enables login. An empty or malformed value stops startup (it never silently disables login). |
   | `JARVIS_AUTH_SIGNING_KEY` | random per start | At least 32 characters. If unset while login is on, a random key is generated at startup, the log says once that sessions reset on every restart, and the key itself is never logged. Set it explicitly so sessions survive restarts. |
   | `JARVIS_AUTH_SESSION_HOURS` | `168` | Absolute session lifetime, 1 to 8760. |
   | `JARVIS_AUTH_COOKIE_SECURE` | `true` | Set `false` only for plain-HTTP loopback development; the server refuses it on a non-loopback bind. Chromium and Firefox still accept Secure cookies on `http://localhost`; Safari does not, so use `false` there. |
   | `JARVIS_TRUSTED_PROXY` | `false` | `1` honors `X-Forwarded-For` and `X-Forwarded-Host` from one trusted proxy. |

4. Start with the supported entry point:

   ```sh
   python -m backend.serve --host 127.0.0.1 --port 8000
   ```

   `backend.serve` refuses a non-loopback `--host` unless login is enabled and `JARVIS_AUTH_COOKIE_SECURE` is not `false`. Treating anything but `localhost` and loopback addresses (including `0.0.0.0`, `::` and hostnames) as non-loopback is deliberate.

## Reverse proxy or tunnel (TLS)

Bind JARVIS to `127.0.0.1` and let a TLS-terminating proxy or tunnel reach it: Tailscale Serve, Cloudflare Tunnel, Caddy, or nginx with a certificate. That keeps the only network-facing port under software that is built for it.

- The proxy must pass the original `Host` header (nginx: `proxy_set_header Host $host;`) so the same-origin check can match `Origin`.
- Set `JARVIS_TRUSTED_PROXY=1` only when JARVIS is reachable *only* through that one proxy. The limiter then uses the **last** `X-Forwarded-For` entry (the one your proxy appended), never the client-supplied first entries. If JARVIS is also reachable directly, any caller can forge that header and dodge per-client limits.
- Do not publish the loopback port some other way (port forwarding, `0.0.0.0` bind) alongside the proxy.

## Deployment checklist

- [ ] TLS terminates in front of JARVIS and plain HTTP is not reachable from outside.
- [ ] JARVIS listens on `127.0.0.1` behind the proxy (or on a private interface with login on and Secure cookies).
- [ ] `JARVIS_AUTH_PASSPHRASE_HASH` and an explicit `JARVIS_AUTH_SIGNING_KEY` are set from a protected location, not the repository.
- [ ] A long unique passphrase; the plaintext is in a password manager, not on the server.
- [ ] `JARVIS_AUTH_COOKIE_SECURE` is left at `true`.
- [ ] `JARVIS_TRUSTED_PROXY=1` only if the proxy is the sole path in.
- [ ] One worker process.
- [ ] From another network: `/` redirects to `/login`, `/api/tasks` returns 401, a wrong passphrase fails, the right one works, logout clears access.
- [ ] Database, vault and log files are readable only by the service user (see [security](security.md)).

## Rotating and recovering

- **Rotate the signing key** (suspected cookie theft, or a lost device): set a new `JARVIS_AUTH_SIGNING_KEY` and restart. Every session on every device ends immediately.
- **Change the passphrase** (suspected phishing, shoulder-surfing or reuse): run the hash helper again, replace `JARVIS_AUTH_PASSPHRASE_HASH`, restart. This also ends all sessions.
- **Lost or stolen device:** rotate the signing key at once, then change the passphrase if the device may have shown or stored it (a password manager on the device counts). Logging out on another device does not revoke a cookie that was copied.
- **Locked out:** wait for the lockout to end (the `429` response carries `Retry-After`), or restart JARVIS to clear the in-memory limiter. If the hash is lost, create a new one; there is no recovery of the old passphrase.
- **Disable login** (loopback use only): unset `JARVIS_AUTH_PASSPHRASE_HASH` and restart. `backend.serve` then refuses non-loopback addresses again.

## Notes for developers

- The enforcement middleware (`backend/auth/middleware.py`) is deny-by-default and exact-match: only the routes above skip the session check, and `/static/` assets other than the two login files need a session. Unmatched paths are answered with the same 401 or 303 as protected ones, so paths are not enumerable without a session.
- Login attempts are counted before the passphrase is checked and refunded on success, so parallel guesses cannot exceed the limit. Malformed requests are rejected first and do not count.
- Other front-end pages do not yet show a logout control or send you back to `/login` on a 401 from a background request; reloading the page does.
- Tests: `tests/test_auth_core.py` (hashing, tokens, limiter, config, startup refusal) and `tests/test_auth_api.py` (enforcement matrix, cookies, CSRF, SSE, logging, hostile input).
