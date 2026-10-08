"""Gemini text adapter using Google's documented content generation REST API."""

import json
import os
import re
from collections.abc import AsyncIterator

import httpx

from backend.core.config import ConfigError
from backend.providers.base import CompletionRequest, CompletionResponse, ProviderError

_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class GeminiProvider:
    name = "gemini"

    def __init__(
        self, *, model: str, api_key: str, client: httpx.AsyncClient | None = None
    ) -> None:
        if not _MODEL_ID.fullmatch(model.strip()):
            raise ConfigError("JARVIS_GEMINI_MODEL must be a model ID")
        if not api_key.strip():
            raise ConfigError("GEMINI_API_KEY is required for the Gemini provider")
        self.model = model.strip()
        self._api_key = api_key.strip()
        self._client = client or httpx.AsyncClient(timeout=30, follow_redirects=False)

    @classmethod
    def from_env(cls) -> "GeminiProvider":
        api_key = os.environ.get("GEMINI_API_KEY", "")
        model = os.environ.get("JARVIS_GEMINI_MODEL", "")
        return cls(model=model, api_key=api_key)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _url(self, *, stream: bool) -> str:
        method = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"{_API_ROOT}/{self.model}:{method}"

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

    @staticmethod
    def _body(request: CompletionRequest) -> dict[str, object]:
        if not request.messages or request.messages[-1].role != "user":
            raise ProviderError("A final user message is required")

        body: dict[str, object] = {"store": False}
        contents: list[dict[str, object]] = []
        for index, message in enumerate(request.messages):
            if message.role == "system":
                if index != 0:
                    raise ProviderError("System message must be first")
                body["systemInstruction"] = {"parts": [{"text": message.content}]}
                continue
            role = "model" if message.role == "assistant" else "user"
            part = {"text": message.content}
            if contents and contents[-1]["role"] == role:
                parts = contents[-1]["parts"]
                assert isinstance(parts, list)
                parts.append(part)
            else:
                contents.append({"role": role, "parts": [part]})
        body["contents"] = contents
        return body

    @staticmethod
    def _content(payload: object) -> tuple[str, str | None]:
        if not isinstance(payload, dict):
            raise ProviderError("Gemini response was invalid")
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return "", None
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ProviderError("Gemini response was invalid")
        reason = candidate.get("finishReason")
        if reason is not None and not isinstance(reason, str):
            raise ProviderError("Gemini response was invalid")
        content = candidate.get("content") or {}
        if not isinstance(content, dict):
            raise ProviderError("Gemini response was invalid")
        parts = content.get("parts") or []
        if not isinstance(parts, list):
            raise ProviderError("Gemini response was invalid")
        text = "".join(
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and part.get("thought") is not True
            and isinstance(part.get("text"), str)
        )
        return text, reason

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = self._body(request)
        try:
            response = await self._client.post(
                self._url(stream=False), headers=self._headers(), json=body
            )
            response.raise_for_status()
            text, reason = self._content(response.json())
            if reason != "STOP":
                raise ProviderError("Gemini response did not complete")
            if not text.strip():
                raise ProviderError("Gemini returned no text")
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("Gemini request failed") from None
        return CompletionResponse(text=text, provider=self.name, model=self.model)

    @staticmethod
    async def _events(response: httpx.Response) -> AsyncIterator[object]:
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                yield json.loads("\n".join(data_lines))
                data_lines.clear()
        if data_lines:
            yield json.loads("\n".join(data_lines))

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        body = self._body(request)
        completed = False
        received_text = False
        try:
            async with self._client.stream(
                "POST", self._url(stream=True), headers=self._headers(), json=body
            ) as response:
                response.raise_for_status()
                async for payload in self._events(response):
                    text, reason = self._content(payload)
                    if reason is not None:
                        if reason != "STOP":
                            raise ProviderError("Gemini response stream failed")
                        completed = True
                    if text:
                        received_text = True
                        yield text
            if not completed:
                raise ProviderError("Gemini response stream ended before completion")
            if not received_text:
                raise ProviderError("Gemini response stream returned no text")
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("Gemini request failed") from None
