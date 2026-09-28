"""OpenAI Responses API implementation of the provider contract."""

import os
from collections.abc import AsyncIterator

from openai import APIError, AsyncOpenAI

from backend.core.config import ConfigError
from backend.providers.base import CompletionRequest, CompletionResponse, ProviderError


class OpenAIResponsesProvider:
    name = "openai"

    def __init__(self, *, model: str, client: AsyncOpenAI) -> None:
        if not model.strip():
            raise ConfigError("JARVIS_OPENAI_MODEL must not be empty")
        self.model = model.strip()
        self._client = client

    @classmethod
    def from_env(cls) -> "OpenAIResponsesProvider":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        model = os.environ.get("JARVIS_OPENAI_MODEL", "")
        if not api_key.strip():
            raise ConfigError("OPENAI_API_KEY is required for the OpenAI provider")
        if not model.strip():
            raise ConfigError("JARVIS_OPENAI_MODEL is required for the OpenAI provider")
        return cls(model=model, client=AsyncOpenAI(api_key=api_key.strip()))

    @staticmethod
    def _input(request: CompletionRequest) -> list[dict[str, str]]:
        if not request.messages:
            raise ProviderError("At least one chat message is required")
        return [{"role": message.role, "content": message.content} for message in request.messages]

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        try:
            response = await self._client.responses.create(
                model=self.model,
                input=self._input(request),
                store=False,
            )
        except APIError as exc:
            raise ProviderError("OpenAI request failed") from exc
        if response.status != "completed":
            raise ProviderError("OpenAI response did not complete")
        if not response.output_text:
            raise ProviderError("OpenAI returned no text")
        return CompletionResponse(text=response.output_text, provider=self.name, model=self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        try:
            events = await self._client.responses.create(
                model=self.model,
                input=self._input(request),
                store=False,
                stream=True,
            )
            completed = False
            received_text = False
            async with events:
                async for event in events:
                    if event.type == "response.output_text.delta":
                        if event.delta:
                            received_text = True
                            yield event.delta
                    elif event.type == "response.completed":
                        completed = True
                    elif event.type in {"response.failed", "response.incomplete", "error"}:
                        raise ProviderError("OpenAI response stream failed")
            if not completed:
                raise ProviderError("OpenAI response stream ended before completion")
            if not received_text:
                raise ProviderError("OpenAI response stream returned no text")
        except APIError as exc:
            raise ProviderError("OpenAI request failed") from exc
