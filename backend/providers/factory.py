"""Build the configured LLM adapter without coupling the chat core to a vendor."""

from backend.core.config import ConfigError, Settings
from backend.providers.base import LLMProvider


def create_provider(settings: Settings) -> LLMProvider | None:
    if settings.llm_provider == "none":
        return None
    if settings.llm_provider == "openai":
        try:
            from backend.providers.openai import OpenAIResponsesProvider
        except ImportError as exc:
            raise ConfigError("Install the optional OpenAI provider dependency") from exc
        return OpenAIResponsesProvider.from_env()
    if settings.llm_provider == "gemini":
        try:
            from backend.providers.gemini import GeminiProvider
        except ImportError as exc:
            raise ConfigError("Install the optional Gemini provider dependency") from exc
        return GeminiProvider.from_env()
    if settings.llm_provider == "groq":
        try:
            from backend.providers.groq import GroqProvider
        except ImportError as exc:
            raise ConfigError("Install the optional Groq provider dependency") from exc
        return GroqProvider.from_env()
    raise ConfigError(f"Unsupported LLM provider: {settings.llm_provider}")
