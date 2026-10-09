# Groq provider

Groq can serve chat through its OpenAI-compatible API. JARVIS uses the documented Chat Completions endpoint (`POST https://api.groq.com/openai/v1/chat/completions`), not the OpenAI Responses API that the `openai` provider uses.

## Setup

```sh
.venv/bin/python -m pip install -e '.[groq]'   # only needs httpx
export JARVIS_LLM_PROVIDER=groq
export GROQ_API_KEY=<your-key>  # set in your own shell; never commit it
export JARVIS_GROQ_MODEL=...   # no default
```

There is deliberately no default model. Model IDs change, so copy one from Groq's own list (`GET https://api.groq.com/openai/v1/models`, with your key in an `Authorization: Bearer` header) and set it explicitly. IDs can contain `/` (for example `owner/model`).

## Behavior

- Same provider contract as the other adapters: `complete` and a streamed `stream` over the existing SSE route.
- A reply counts only when it finishes with `stop` and has text. A truncated reply (`length`), an empty reply, a stream that ends before completion, an HTTP error, or malformed data all raise a `ProviderError` with a fixed message; provider error bodies, the key and the model are never echoed.
- The key is sent only in the `Authorization` header from the server. If `JARVIS_MEMORY_VAULT_PATH` is set, matching reviewed memory excerpts are sent to Groq like any other provider.
- Research and the router use the default provider (`JARVIS_LLM_PROVIDER`), so they use Groq when it is the default.

## Model selection and doctor

`JARVIS_MODEL_CHOICES` accepts `groq:<model>` entries (see [model selection](model-select.md)); an entry is selectable when `GROQ_API_KEY` is set and `httpx` is installed. `python -m backend.doctor` reports the chat row for `groq` (key, model and dependency) without printing values.
