"""Groq text adapter using its OpenAI-compatible Chat Completions REST API."""

import json
import os
import re
from collections.abc import AsyncIterator

import httpx

from backend.core.config import ConfigError
from backend.providers.base import CompletionRequest, CompletionResponse, ProviderError

_API_ROOT = "https://api.groq.com/openai/v1"
# Groq model IDs may contain "/" (for example "owner/model"); copy them from the models list.
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}\Z")


class GroqProvider:
    name = "groq"

    def __init__(
        self, *, model: str, api_key: str, client: httpx.AsyncClient | None = None
    ) -> None:
        if not _MODEL_ID.fullmatch(model.strip()):
            raise ConfigError("JARVIS_GROQ_MODEL must be a model ID")
        if not api_key.strip():
            raise ConfigError("GROQ_API_KEY is required for the Groq provider")
        self.model = model.strip()
        self._api_key = api_key.strip()
        self._client = client or httpx.AsyncClient(timeout=30, follow_redirects=False)

    @classmethod
    def from_env(cls) -> "GroqProvider":
        api_key = os.environ.get("GROQ_API_KEY", "")
        model = os.environ.get("JARVIS_GROQ_MODEL", "")
        return cls(model=model, api_key=api_key)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def _body(self, request: CompletionRequest, *, stream: bool) -> dict[str, object]:
        if not request.messages:
            raise ProviderError("At least one chat message is required")
        body: dict[str, object] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if stream:
            body["stream"] = True
        return body

    @staticmethod
    def _choice(payload: object) -> dict[str, object] | None:
        if not isinstance(payload, dict):
            raise ProviderError("Groq response was invalid")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderError("Groq response was invalid")
        return choice

    @classmethod
    def _text(cls, payload: object, field: str) -> tuple[str, str | None]:
        choice = cls._choice(payload)
        if choice is None:
            return "", None
        reason = choice.get("finish_reason")
        if reason is not None and not isinstance(reason, str):
            raise ProviderError("Groq response was invalid")
        container = choice.get(field) or {}
        if not isinstance(container, dict):
            raise ProviderError("Groq response was invalid")
        content = container.get("content")
        if content is None:
            return "", reason
        if not isinstance(content, str):
            raise ProviderError("Groq response was invalid")
        return content, reason

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = self._body(request, stream=False)
        try:
            response = await self._client.post(
                f"{_API_ROOT}/chat/completions", headers=self._headers(), json=body
            )
            response.raise_for_status()
            text, reason = self._text(response.json(), "message")
            if reason != "stop":
                raise ProviderError("Groq response did not complete")
            if not text.strip():
                raise ProviderError("Groq returned no text")
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("Groq request failed") from None
        return CompletionResponse(text=text, provider=self.name, model=self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        body = self._body(request, stream=True)
        completed = False
        received_text = False
        try:
            async with self._client.stream(
                "POST", f"{_API_ROOT}/chat/completions", headers=self._headers(), json=body
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    payload = json.loads(data)
                    if isinstance(payload, dict) and "error" in payload:
                        raise ProviderError("Groq response stream failed")
                    text, reason = self._text(payload, "delta")
                    if reason is not None:
                        if reason != "stop":
                            raise ProviderError("Groq response stream failed")
                        completed = True
                    if text:
                        received_text = True
                        yield text
            if not completed:
                raise ProviderError("Groq response stream ended before completion")
            if not received_text:
                raise ProviderError("Groq response stream returned no text")
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("Groq request failed") from None
