"""Owner-selectable chat models: a server-side allowlist, never a free-form client value.

The allowlist is ``Settings.model_choices`` (``JARVIS_MODEL_CHOICES``). A client may only send one
of its exact entries (``provider:model``); anything else is refused before any provider is
touched. Credentials stay in the environment (``OPENAI_API_KEY`` / ``GEMINI_API_KEY`` /
``GROQ_API_KEY``): an entry
is selectable only when its provider's key is present and its package importable. Providers are
built lazily, once per entry, with the same classes the default provider uses. Only the research
and router paths do not use this: they keep the default provider.
"""

import importlib.util
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from backend.core.config import ConfigError
from backend.providers.base import LLMProvider

_KEY_ENV = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}
_MODULE = {"openai": "openai", "gemini": "httpx", "groq": "httpx"}


class ModelChoiceError(Exception):
    """A chat request named a model the server will not use."""


class UnknownModelChoice(ModelChoiceError):
    """Not an entry of the allowlist."""


class ModelUnavailable(ModelChoiceError):
    """An allowlist entry whose provider has no key, is not installed or failed to build."""


@dataclass(frozen=True)
class ModelOption:
    id: str
    provider: str
    model: str
    available: bool
    is_default: bool

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "available": self.available,
            "is_default": self.is_default,
        }


def split_choice(entry: str) -> tuple[str, str]:
    provider, _, model = entry.partition(":")
    return provider, model


def installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def provider_ready(provider: str, env: Mapping[str, str]) -> bool:
    """Key present and package importable. Reports a fact only; never returns the key."""
    return bool(env.get(_KEY_ENV[provider], "").strip()) and installed(_MODULE[provider])


def build_provider(provider: str, model: str, env: Mapping[str, str]) -> LLMProvider:
    key = env.get(_KEY_ENV[provider], "").strip()
    if not key:
        raise ModelUnavailable("provider key is not configured")
    try:
        if provider == "openai":
            from openai import AsyncOpenAI

            from backend.providers.openai import OpenAIResponsesProvider

            return OpenAIResponsesProvider(model=model, client=AsyncOpenAI(api_key=key))
        if provider == "groq":
            from backend.providers.groq import GroqProvider

            return GroqProvider(model=model, api_key=key)
        from backend.providers.gemini import GeminiProvider

        return GeminiProvider(model=model, api_key=key)
    except ImportError as exc:
        raise ModelUnavailable("provider package is not installed") from exc
    except ConfigError as exc:
        raise ModelUnavailable("provider could not be configured") from exc


Builder = Callable[[str, str], LLMProvider]


class ModelRegistry:
    def __init__(
        self,
        choices: tuple[str, ...],
        default: LLMProvider,
        *,
        env: Mapping[str, str] | None = None,
        builder: Builder | None = None,
        ready: Callable[[str], bool] | None = None,
    ) -> None:
        self._choices = choices
        self._default = default
        self._env = os.environ if env is None else env
        self._builder = builder or (
            lambda provider, model: build_provider(provider, model, self._env)
        )
        self._ready = ready or (lambda provider: provider_ready(provider, self._env))
        self._cache: dict[str, LLMProvider] = {}
        self.default_id = (
            f"{getattr(default, 'name', '')}:{getattr(default, 'model', '')}"
        )

    def __bool__(self) -> bool:
        return bool(self._choices)

    def _available(self, entry: str) -> bool:
        if entry == self.default_id or entry in self._cache:
            return True
        return self._ready(split_choice(entry)[0])

    def options(self) -> list[ModelOption]:
        result = []
        for entry in self._choices:
            provider, model = split_choice(entry)
            is_default = entry == self.default_id
            result.append(
                ModelOption(entry, provider, model, self._available(entry), is_default)
            )
        return result

    def provider_for(self, choice: str | None) -> LLMProvider:
        """The provider for one request. ``None`` is the default; anything not in the allowlist
        is refused without being looked at any further."""
        if choice is None:
            return self._default
        if choice not in self._choices:
            raise UnknownModelChoice("model choice is not allowed")
        if choice == self.default_id:
            return self._default
        cached = self._cache.get(choice)
        if cached is not None:
            return cached
        if not self._ready(split_choice(choice)[0]):
            raise ModelUnavailable("model choice is not available")
        provider, model = split_choice(choice)
        built = self._builder(provider, model)
        self._cache[choice] = built
        return built

    async def aclose(self) -> None:
        for built in self._cache.values():
            close = getattr(built, "aclose", None)
            if close is not None:
                await close()
        self._cache.clear()
