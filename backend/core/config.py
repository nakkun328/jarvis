"""Validated, OS-independent application configuration."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

VALID_ENVIRONMENTS = {"development", "test", "production"}
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


@dataclass(frozen=True)
class Settings:
    environment: str
    log_level: str
    data_dir: Path
    database_path: Path
    llm_provider: str = "openai"
    llm_model: str = "gpt-5.5"
    openai_api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(
        cls, env_file: Path | None = None, environ: dict[str, str] | None = None
    ) -> "Settings":
        root = Path.cwd()
        values = dict(dotenv_values(env_file or root / ".env"))
        values.update(os.environ if environ is None else environ)
        environment = str(values.get("JARVIS_ENVIRONMENT") or "development").lower()
        log_level = str(values.get("JARVIS_LOG_LEVEL") or "INFO").upper()
        if environment not in VALID_ENVIRONMENTS:
            raise ValueError(
                "JARVIS_ENVIRONMENT must be development, test, or production"
            )
        if log_level not in VALID_LOG_LEVELS:
            raise ValueError("JARVIS_LOG_LEVEL is invalid")
        data_dir = _path(str(values.get("JARVIS_DATA_DIR") or "data"), root)
        database_path = _path(
            str(values.get("JARVIS_DATABASE_PATH") or data_dir / "jarvis.sqlite3"),
            root,
        )
        llm_provider = str(values.get("JARVIS_LLM_PROVIDER") or "openai").lower()
        llm_model = str(values.get("JARVIS_LLM_MODEL", "gpt-5.5")).strip()
        if not llm_model:
            raise ValueError("JARVIS_LLM_MODEL must not be empty")
        api_key = str(values.get("OPENAI_API_KEY") or "").strip() or None
        return cls(
            environment,
            log_level,
            data_dir,
            database_path,
            llm_provider,
            llm_model,
            api_key,
        )
