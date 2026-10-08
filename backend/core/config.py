"""Runtime configuration sourced from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from backend.auth.passwords import PasswordHashError, parse_hash


class ConfigError(ValueError):
    """Configuration is missing or invalid."""


_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_LLM_PROVIDERS = frozenset({"none", "openai", "gemini"})
_SEARCH_PROVIDERS = frozenset({"none", "tavily"})
_DEFAULT_SEARCH_MONTHLY_LIMIT = 800
_MAX_SEARCH_MONTHLY_LIMIT = 1_000_000
# off: no router (default). rule: the offline keyword baseline. llm: one extra short call to
# the configured chat provider per turn (docs/router.md "Wiring").
_ROUTER_MODES = frozenset({"off", "rule", "llm"})
# The research level a routed chat turn starts. Quick is the cheaper one (and the default).
_CHAT_RESEARCH_LEVELS = frozenset({"quick", "standard"})
_MIN_SIGNING_KEY_CHARS = 32
_MAX_SESSION_HOURS = 24 * 365
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a whole number") from None


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_level: str = "INFO"
    llm_provider: str = "none"
    memory_vault_path: Path | None = None
    personality_path: Path | None = None
    router: str = "off"
    # Single-owner login. Setting the passphrase hash turns authentication on. The credentials
    # are excluded from repr so they cannot leak through logs or assertion messages.
    auth_passphrase_hash: str | None = field(default=None, repr=False)
    auth_signing_key: str | None = field(default=None, repr=False)
    auth_session_hours: int = 168
    auth_cookie_secure: bool = True
    trusted_proxy: bool = False
    # Web search is off by default. The key is excluded from repr like the other credentials.
    search_provider: str = "none"
    search_key: str | None = field(default=None, repr=False)
    search_monthly_limit: int = _DEFAULT_SEARCH_MONTHLY_LIMIT
    # Starting a web research from the Research screen is off by default. It also needs a
    # search provider and a chat provider; with any of the three missing it stays unavailable.
    research_enabled: bool = False
    # Which research a chat turn starts when the router chooses research (and a router, research,
    # a search provider and a chat provider are all configured). A fixed enum, not a free value.
    chat_research_level: str = "quick"

    @property
    def auth_enabled(self) -> bool:
        return self.auth_passphrase_hash is not None

    def __post_init__(self) -> None:
        if not str(self.db_path).strip():
            raise ConfigError("JARVIS_DB_PATH must not be empty")
        if self.log_level not in _LOG_LEVELS:
            raise ConfigError(f"JARVIS_LOG_LEVEL must be one of: {', '.join(sorted(_LOG_LEVELS))}")
        if self.llm_provider not in _LLM_PROVIDERS:
            raise ConfigError(
                f"JARVIS_LLM_PROVIDER must be one of: {', '.join(sorted(_LLM_PROVIDERS))}"
            )
        if self.search_provider not in _SEARCH_PROVIDERS:
            raise ConfigError(
                f"JARVIS_SEARCH_PROVIDER must be one of: {', '.join(sorted(_SEARCH_PROVIDERS))}"
            )
        if self.search_provider != "none" and not (self.search_key or "").strip():
            raise ConfigError("JARVIS_SEARCH_API_KEY is required when a search provider is set")
        if (
            isinstance(self.search_monthly_limit, bool)
            or not isinstance(self.search_monthly_limit, int)
            or not 1 <= self.search_monthly_limit <= _MAX_SEARCH_MONTHLY_LIMIT
        ):
            raise ConfigError(
                f"JARVIS_SEARCH_MONTHLY_LIMIT must be between 1 and {_MAX_SEARCH_MONTHLY_LIMIT}"
            )
        if not isinstance(self.research_enabled, bool):
            raise ConfigError("JARVIS_RESEARCH_ENABLED must be true or false")
        if self.chat_research_level not in _CHAT_RESEARCH_LEVELS:
            raise ConfigError(
                "JARVIS_CHAT_RESEARCH_LEVEL must be one of: "
                f"{', '.join(sorted(_CHAT_RESEARCH_LEVELS))}"
            )
        if self.router not in _ROUTER_MODES:
            raise ConfigError(f"JARVIS_ROUTER must be one of: {', '.join(sorted(_ROUTER_MODES))}")
        if self.memory_vault_path is not None and not str(self.memory_vault_path).strip():
            raise ConfigError("JARVIS_MEMORY_VAULT_PATH must not be empty")
        if self.personality_path is not None and not str(self.personality_path).strip():
            raise ConfigError("JARVIS_PERSONALITY_PATH must not be empty")
        if self.auth_passphrase_hash is not None:
            try:
                parse_hash(self.auth_passphrase_hash)
            except PasswordHashError:
                raise ConfigError(
                    "JARVIS_AUTH_PASSPHRASE_HASH is not a valid hash; "
                    "create one with: python -m backend.auth.hash_password"
                ) from None
        if (
            self.auth_signing_key is not None
            and len(self.auth_signing_key) < _MIN_SIGNING_KEY_CHARS
        ):
            raise ConfigError(
                f"JARVIS_AUTH_SIGNING_KEY must be at least {_MIN_SIGNING_KEY_CHARS} characters"
            )
        if not 1 <= self.auth_session_hours <= _MAX_SESSION_HOURS:
            raise ConfigError(
                f"JARVIS_AUTH_SESSION_HOURS must be between 1 and {_MAX_SESSION_HOURS}"
            )

    @classmethod
    def from_env(cls) -> "Settings":
        raw_path = os.environ.get("JARVIS_DB_PATH", "data/jarvis.sqlite3")
        if not raw_path.strip():
            raise ConfigError("JARVIS_DB_PATH must not be empty")
        vault_path = os.environ.get("JARVIS_MEMORY_VAULT_PATH")
        if vault_path is not None and not vault_path.strip():
            raise ConfigError("JARVIS_MEMORY_VAULT_PATH must not be empty")
        personality_path = os.environ.get("JARVIS_PERSONALITY_PATH")
        if personality_path is not None and not personality_path.strip():
            raise ConfigError("JARVIS_PERSONALITY_PATH must not be empty")
        passphrase_hash = os.environ.get("JARVIS_AUTH_PASSPHRASE_HASH")
        if passphrase_hash is not None and not passphrase_hash.strip():
            # Fail closed: an empty value must never silently mean "authentication off".
            raise ConfigError("JARVIS_AUTH_PASSPHRASE_HASH must not be empty")
        signing_key = os.environ.get("JARVIS_AUTH_SIGNING_KEY")
        if signing_key is not None and not signing_key.strip():
            raise ConfigError("JARVIS_AUTH_SIGNING_KEY must not be empty")
        search_key = os.environ.get("JARVIS_SEARCH_API_KEY")
        if search_key is not None and not search_key.strip():
            raise ConfigError("JARVIS_SEARCH_API_KEY must not be empty")
        return cls(
            db_path=Path(raw_path).expanduser(),
            log_level=os.environ.get("JARVIS_LOG_LEVEL", "INFO").upper(),
            llm_provider=os.environ.get("JARVIS_LLM_PROVIDER", "none").lower(),
            router=os.environ.get("JARVIS_ROUTER", "off").strip().lower(),
            memory_vault_path=Path(vault_path).expanduser() if vault_path is not None else None,
            personality_path=(
                Path(personality_path).expanduser() if personality_path is not None else None
            ),
            auth_passphrase_hash=passphrase_hash.strip() if passphrase_hash is not None else None,
            auth_signing_key=signing_key.strip() if signing_key is not None else None,
            auth_session_hours=_env_int("JARVIS_AUTH_SESSION_HOURS", 168),
            auth_cookie_secure=_env_bool("JARVIS_AUTH_COOKIE_SECURE", True),
            trusted_proxy=_env_bool("JARVIS_TRUSTED_PROXY", False),
            search_provider=os.environ.get("JARVIS_SEARCH_PROVIDER", "none").strip().lower(),
            search_key=search_key.strip() if search_key is not None else None,
            search_monthly_limit=_env_int(
                "JARVIS_SEARCH_MONTHLY_LIMIT", _DEFAULT_SEARCH_MONTHLY_LIMIT
            ),
            research_enabled=_env_bool("JARVIS_RESEARCH_ENABLED", False),
            chat_research_level=(
                os.environ.get("JARVIS_CHAT_RESEARCH_LEVEL", "quick").strip().lower()
            ),
        )
