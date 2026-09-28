"""Select the configured provider without coupling callers to an API."""

from backend.core.config import Settings
from backend.providers.base import LLMProvider
from backend.providers.openai import OpenAIProvider


def create_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for the OpenAI provider")
        return OpenAIProvider(settings.openai_api_key, settings.llm_model)
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")
