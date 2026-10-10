# Devices screen

`GET /devices` serves a read-only page, backed by `GET /api/devices`. It shows only what the
server can truthfully know today. Like every page except the login page it needs a session when
login is on (it is not on the public allowlist).

## What it shows

- **This device (current connection):** browser and OS family, summarised from the request's
  User-Agent (the raw string and the client address are never returned or stored); whether the
  connection is `local` (loopback peer), `remote`, or `proxied` (behind `JARVIS_TRUSTED_PROXY`,
  where the peer address says nothing); and the request scheme. The browser itself adds secure
  context, installed-as-app (standalone display mode) and online state; these never leave the
  browser.
- **Server status:** whether login is on, the chat and search provider names (`none` when unset),
  and feature switches (router mode, research, shell tool, model selection). Keys, paths and
  endpoints are never included.
- **Registered devices and last connection:** an explicit "not connected / not implemented" notice.

## What it does not show

There is no device registry. Login uses stateless signed cookies, so the server keeps no list of
sessions or devices and records no last-connection time. The screen therefore cannot list devices
or their last connection state, and it does not invent any. The API says so with
`registered_devices: {"available": false, "reason": "no_device_registry", "devices": []}`.

## API contract

```json
{
  "current": {"browser": "Firefox", "os": "Linux", "connection": "local", "scheme": "http"},
  "server": {
    "login_enabled": false,
    "providers": {"chat": "none", "search": "none"},
    "features": {"router": "off", "research": false, "shell": false, "model_choices": false}
  },
  "registered_devices": {"available": false, "reason": "no_device_registry", "devices": []}
}
```

GET only; responses are `Cache-Control: no-store`.

## Design for a real device list (not built)

A list with last connection needs storage, so it needs a schema migration:

- A `devices` table: random device id, label, browser/OS family, created and `last_seen_at`
  (UTC), `revoked_at`.
- The session cookie would carry the device id (signed as today); the auth middleware would
  update `last_seen_at` at most once per interval to avoid a write per request.
- `GET /api/devices` would fill `registered_devices.devices` and set `available` to true; a
  revoke action (POST with the confirm header, as approvals do) would be a separate decision.
- Privacy: store families, not raw User-Agent strings or IP addresses, with a retention limit.

The page's `normalizeDevices` currently rejects `available: true`, so enabling this is a
deliberate change in both the API and the screen.
