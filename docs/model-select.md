# Chat model selection

The owner can pick the chat model from a small selector (label "モデル") in the chat header. It is off by default.

## Server configuration

```sh
JARVIS_MODEL_CHOICES=openai:gpt-6-luna,gemini:gemini-2.5-flash
```

- A comma list of `provider:model`. `provider` is `openai`, `gemini` or `groq` (lowercase); `model` is a model ID (letters, digits, `.`, `_`, `-`, at most 64 characters; `groq` IDs may also contain `/`, e.g. `owner/model`). At most 8 entries, no duplicates. A malformed entry, unknown provider or duplicate makes the server refuse to start (`ConfigError`, without echoing the value).
- Unset or blank: no selector, `GET /api/models` returns `[]`, and chat behaves exactly as before.
- Credentials stay in the environment only (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`). An entry is selectable only when its provider's key is present and its package is installed. Nothing about keys is ever returned.
- The default stays what `JARVIS_LLM_PROVIDER` and `JARVIS_OPENAI_MODEL` / `JARVIS_GEMINI_MODEL` / `JARVIS_GROQ_MODEL` say. The list needs a configured default provider; without one it is ignored (the doctor reports it).

## Behavior

- `GET /api/models` returns `[{id, provider, model, available, is_default}]`, where `id` is the exact allowlist entry. `is_default` marks the entry that equals the default `provider:model`.
- `POST /api/chat` and `/api/chat/stream` accept an optional `model_choice`, which must be exactly one allowlist entry. Anything else is refused before any provider is contacted: `400 {"detail": "unknown model choice"}`; an allowlisted entry whose provider is not usable gives `503 {"detail": "model choice unavailable"}`. Without the field the default model answers. Free-form model names from clients are never forwarded.
- Providers for chosen entries are built on first use and cached; they are closed when the app stops.
- The choice applies per request to the chat answer only. The router and web research keep using the default provider.
- The log records only the allowlist string (`chat.model_selected`), never a client-supplied value. The reply's `provider` / `model` fields name the model that answered.

## Frontend

The selector lists the server's entries (unavailable ones are disabled and marked), plus "既定のモデル" for the default. The choice is remembered in `localStorage` (failures are ignored) and sent with each chat request. If the remembered entry is no longer offered or usable, the default is used silently. The control is built with text nodes only, uses the same-origin session like other API calls, and is hidden when the server offers no list.

`python -m backend.doctor` has a `models` row (`OFF` / `OK` / `INCOMPLETE`, counts only).
