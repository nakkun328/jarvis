# Using JARVIS from other devices (phone, tablet, another computer)

The recommended way: keep JARVIS on loopback (`127.0.0.1`) on the machine that runs it, turn on the
single-owner login, and let **Tailscale Serve** deliver it over HTTPS to your own devices only. Nothing
is published to the internet and JARVIS itself never listens on a public address. Background and the
threat model are in [auth.md](auth.md).

## Why this way

- JARVIS does not speak TLS. A private network plus a TLS-terminating proxy keeps the only network-facing
  part under software built for it (see [auth.md](auth.md#reverse-proxy-or-tunnel-tls)).
- Only devices signed in to your Tailscale account can reach it; the login passphrase protects it
  further. Tailscale Serve provides a real HTTPS certificate for the machine's `ts.net` name.
- `python -m backend.serve` refuses a non-loopback `--host` unless login is on and cookies are Secure, so
  a misconfiguration fails closed.

## Steps

1. **Turn on the login.** Create the passphrase hash (hidden prompt; only the hash is printed) and a
   signing key (at least 32 characters), and give both to the server as environment variables from a
   protected, non-repository place (for example a file readable only by you that your start script loads):

   ```sh
   .venv/bin/python -m backend.auth.hash_password
   .venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   | Variable | Value |
   | --- | --- |
   | `JARVIS_AUTH_PASSPHRASE_HASH` | the hash from the first command (contains `$`: quote it in a shell) |
   | `JARVIS_AUTH_SIGNING_KEY` | the random string from the second command |
   | `JARVIS_TRUSTED_PROXY` | `1` (JARVIS is reachable only through the proxy, see step 3) |

   Leave `JARVIS_AUTH_COOKIE_SECURE` at its default (`true`). Never commit these values or paste them into
   chat, issues or pull requests.

2. **Install Tailscale** on the machine that runs JARVIS and on each device you want to use, and sign in to
   the same (personal) account on all of them. In the Tailscale admin console enable *HTTPS Certificates*
   (DNS settings).

3. **Start JARVIS on loopback, then serve it on your tailnet:**

   ```sh
   .venv/bin/python -m backend.doctor      # login should be reported as enabled
   .venv/bin/python -m backend.serve --host 127.0.0.1 --port 8000
   tailscale serve --bg 8000
   ```

   Tailscale prints an address like `https://<machine>.<tailnet>.ts.net`.

4. **Open that address** on a device that has Tailscale turned on, sign in with the passphrase, and add the
   page to the home screen if you want an app-like icon (the site is a PWA, see [web-shell.md](web-shell.md)).

## Do and don't

- Use `tailscale serve`, **not** `tailscale funnel` (Funnel publishes the service to the whole internet).
- Do not also expose port 8000 another way (port forwarding, `--host 0.0.0.0`) next to the proxy;
  `JARVIS_TRUSTED_PROXY=1` is only safe when the proxy is the sole path in.
- The machine must be awake and `backend.serve` running for other devices to connect.
- Lost or stolen device: set a new `JARVIS_AUTH_SIGNING_KEY` and restart; every session on every device
  ends. Change the passphrase by creating a new hash.
- Anyone with the passphrase and access to your tailnet can use JARVIS, including its memory and the LLM
  providers behind it.

## Other options

Cloudflare Tunnel, Caddy or nginx with a certificate work the same way: bind JARVIS to `127.0.0.1` and let
the proxy terminate TLS. Use them when a device cannot run Tailscale. Add an access policy in front (for
example Cloudflare Access) rather than relying on the passphrase alone when the address is public.
