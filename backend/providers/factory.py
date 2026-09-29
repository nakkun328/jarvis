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
    raise ConfigError(f"Unsupported LLM provider: {settings.llm_provider}")
