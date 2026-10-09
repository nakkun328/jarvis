"""Runtime configuration sourced from environment variables."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.auth.passwords import PasswordHashError, parse_hash


class ConfigError(ValueError):
    """Configuration is missing or invalid."""


_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_LLM_PROVIDERS = frozenset({"none", "openai", "gemini", "groq"})
_SEARCH_PROVIDERS = frozenset({"none", "tavily"})
_DEFAULT_SEARCH_MONTHLY_LIMIT = 800
_MAX_SEARCH_MONTHLY_LIMIT = 1_000_000
# off: no router (default). rule: the offline keyword baseline. llm: one extra short call to
# the configured chat provider per turn (docs/router.md "Wiring").
_ROUTER_MODES = frozenset({"off", "rule", "llm"})
# The research level a routed chat turn starts. Quick is the cheaper one (and the default).
_CHAT_RESEARCH_LEVELS = frozenset({"quick", "standard"})
# Chat model allowlist (JARVIS_MODEL_CHOICES): at most this many `provider:model` entries.
_MODEL_CHOICE_PROVIDERS = frozenset({"openai", "gemini", "groq"})
MAX_MODEL_CHOICES = 8
_MODEL_CHOICE_ENTRY = re.compile(
    r"(?:(?:openai|gemini):[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
    r"|groq:[A-Za-z0-9][A-Za-z0-9._/-]{0,63})\Z"  # Groq model IDs may contain "/"
)
_MIN_SIGNING_KEY_CHARS = 32
_SHELL_COMMAND_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MAX_SHELL_COMMANDS = 16
DEFAULT_SHELL_COMMANDS = ("ls", "cat", "git")
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


def parse_model_choices(raw: str) -> tuple[str, ...]:
    """The allowlist from a comma list of ``provider:model``. Blank means no choices.

    Raises ConfigError for a malformed or empty entry, an unknown provider, a duplicate or more
    than MAX_MODEL_CHOICES entries. The message never repeats the offending value.
    """
    if not raw.strip():
        return ()
    entries: list[str] = []
    for part in raw.split(","):
        candidate = part.strip()
        match = _MODEL_CHOICE_ENTRY.fullmatch(candidate)
        if match is None:
            raise ConfigError(
                "JARVIS_MODEL_CHOICES entries must be provider:model with provider one of: "
                f"{', '.join(sorted(_MODEL_CHOICE_PROVIDERS))}"
            )
        entries.append(candidate)
    if len(entries) != len(set(entries)):
        raise ConfigError("JARVIS_MODEL_CHOICES must not contain duplicates")
    if len(entries) > MAX_MODEL_CHOICES:
        raise ConfigError(f"JARVIS_MODEL_CHOICES allows at most {MAX_MODEL_CHOICES} entries")
    return tuple(entries)


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
    # The owner-selectable chat models (JARVIS_MODEL_CHOICES): an allowlist of `provider:model`
    # strings. Empty (the default) means no selector and exactly the single configured model.
    model_choices: tuple[str, ...] = ()
    # Structured shell tool (docs/tool-shell.md). Off by default; it needs an execution root too.
    # The command names pick entries of the fixed allowlist table, they are never command lines.
    shell_enabled: bool = False
    shell_root: Path | None = None
    shell_timeout_seconds: float = 30.0
    shell_max_output_bytes: int = 32_768
    shell_commands: tuple[str, ...] = DEFAULT_SHELL_COMMANDS

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
        if not isinstance(self.model_choices, tuple) or any(
            not isinstance(entry, str) for entry in self.model_choices
        ):
            raise ConfigError("JARVIS_MODEL_CHOICES is invalid")
        if self.model_choices and parse_model_choices(",".join(self.model_choices)) != (
            self.model_choices
        ):
            raise ConfigError("JARVIS_MODEL_CHOICES is invalid")
        if not isinstance(self.shell_enabled, bool):
            raise ConfigError("JARVIS_SHELL_ENABLED must be true or false")
        if self.shell_root is not None and not str(self.shell_root).strip():
            raise ConfigError("JARVIS_SHELL_ROOT must not be empty")
        if (
            isinstance(self.shell_timeout_seconds, bool)
            or not isinstance(self.shell_timeout_seconds, int | float)
            or not 1 <= self.shell_timeout_seconds <= 600
        ):
            raise ConfigError("JARVIS_SHELL_TIMEOUT_SECONDS must be between 1 and 600")
        if (
            isinstance(self.shell_max_output_bytes, bool)
            or not isinstance(self.shell_max_output_bytes, int)
            or not 1 <= self.shell_max_output_bytes <= 65_536
        ):
            raise ConfigError("JARVIS_SHELL_MAX_OUTPUT_BYTES must be between 1 and 65536")
        names = tuple(self.shell_commands)
        object.__setattr__(self, "shell_commands", names)
        if (
            not 1 <= len(names) <= _MAX_SHELL_COMMANDS
            or len(set(names)) != len(names)
            or not all(isinstance(n, str) and _SHELL_COMMAND_NAME.fullmatch(n) for n in names)
        ):
            raise ConfigError("JARVIS_SHELL_COMMANDS must list 1-16 distinct lowercase names")
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
        shell_root = os.environ.get("JARVIS_SHELL_ROOT")
        if shell_root is not None and not shell_root.strip():
            raise ConfigError("JARVIS_SHELL_ROOT must not be empty")
        raw_commands = os.environ.get("JARVIS_SHELL_COMMANDS")
        shell_commands = (
            tuple(p.strip().lower() for p in raw_commands.split(","))
            if raw_commands is not None
            else DEFAULT_SHELL_COMMANDS
        )
        try:
            shell_timeout = float(os.environ.get("JARVIS_SHELL_TIMEOUT_SECONDS", "30").strip())
        except ValueError:
            raise ConfigError("JARVIS_SHELL_TIMEOUT_SECONDS must be a number") from None
        return cls(
            shell_enabled=_env_bool("JARVIS_SHELL_ENABLED", False),
            shell_root=Path(shell_root.strip()).expanduser() if shell_root is not None else None,
            shell_timeout_seconds=shell_timeout,
            shell_max_output_bytes=_env_int("JARVIS_SHELL_MAX_OUTPUT_BYTES", 32_768),
            shell_commands=shell_commands,
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
            model_choices=parse_model_choices(os.environ.get("JARVIS_MODEL_CHOICES", "")),
        )
