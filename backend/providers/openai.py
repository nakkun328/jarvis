"""OpenAI Responses HTTP API adapter."""

from collections.abc import Sequence

import httpx

from backend.providers.base import Generation, Message, ProviderError

RESPONSES_URL = "https://api.openai.com/v1/responses"
REQUEST_TIMEOUT_SECONDS = 30.0


class OpenAIProvider:
    def __init__(
        self, api_key: str, model: str, client: httpx.Client | None = None
    ) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required for the OpenAI provider")
        if not model.strip():
            raise ValueError("LLM model must not be empty")
        self.model = model
        self._api_key = api_key
        self._client = client

    def generate(self, messages: Sequence[Message]) -> Generation:
        if not messages:
            raise ValueError("At least one message is required")
        try:
            post = self._client.post if self._client is not None else httpx.post
            response = post(
                RESPONSES_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self.model,
                    "input": [
                        {"role": message.role, "content": message.content}
                        for message in messages
                    ],
                    "store": False,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise ProviderError("LLM request failed") from None
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            raise ProviderError("LLM response was not completed")
        output = payload.get("output")
        if not isinstance(output, list):
            raise ProviderError("LLM response has no output")
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    value = part.get("text")
                    if isinstance(value, str):
                        texts.append(value)
        text = "".join(texts)
        if not text.strip():
            raise ProviderError("LLM returned no text")
        return Generation(text=text, provider="openai", model=self.model)
