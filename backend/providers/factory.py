"""Build the configured LLM adapter without coupling the chat core to a vendor."""

from backend.core.config import ConfigError, Settings
from backend.providers.base import LLMProvider


def create_provider(
    settings: Settings, *, gemini_max_output_tokens: int | None = None
) -> LLMProvider | None:
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
        return GeminiProvider.from_env(max_output_tokens=gemini_max_output_tokens)
    raise ConfigError(f"Unsupported LLM provider: {settings.llm_provider}")
