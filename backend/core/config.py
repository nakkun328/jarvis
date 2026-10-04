"""Runtime configuration sourced from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Configuration is missing or invalid."""


_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_LLM_PROVIDERS = frozenset({"none", "openai", "gemini"})


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_level: str = "INFO"
    llm_provider: str = "none"
    memory_vault_path: Path | None = None

    def __post_init__(self) -> None:
        if not str(self.db_path).strip():
            raise ConfigError("JARVIS_DB_PATH must not be empty")
        if self.log_level not in _LOG_LEVELS:
            raise ConfigError(f"JARVIS_LOG_LEVEL must be one of: {', '.join(sorted(_LOG_LEVELS))}")
        if self.llm_provider not in _LLM_PROVIDERS:
            raise ConfigError(
                f"JARVIS_LLM_PROVIDER must be one of: {', '.join(sorted(_LLM_PROVIDERS))}"
            )
        if self.memory_vault_path is not None and not str(self.memory_vault_path).strip():
            raise ConfigError("JARVIS_MEMORY_VAULT_PATH must not be empty")

    @classmethod
    def from_env(cls) -> "Settings":
        raw_path = os.environ.get("JARVIS_DB_PATH", "data/jarvis.sqlite3")
        if not raw_path.strip():
            raise ConfigError("JARVIS_DB_PATH must not be empty")
        vault_path = os.environ.get("JARVIS_MEMORY_VAULT_PATH")
        if vault_path is not None and not vault_path.strip():
            raise ConfigError("JARVIS_MEMORY_VAULT_PATH must not be empty")
        return cls(
            db_path=Path(raw_path).expanduser(),
            log_level=os.environ.get("JARVIS_LOG_LEVEL", "INFO").upper(),
            llm_provider=os.environ.get("JARVIS_LLM_PROVIDER", "none").lower(),
            memory_vault_path=Path(vault_path).expanduser() if vault_path is not None else None,
        )
