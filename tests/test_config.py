from pathlib import Path

import pytest

from backend.core.config import Settings


def test_environment_overrides_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("JARVIS_ENVIRONMENT=production\nJARVIS_LOG_LEVEL=WARNING\n")
    settings = Settings.from_env(env_file, {"JARVIS_ENVIRONMENT": "test"})
    assert settings.environment == "test"
    assert settings.log_level == "WARNING"


def test_relative_paths_follow_working_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings.from_env(tmp_path / "missing.env", {})
    assert settings.data_dir == tmp_path / "data"
    assert settings.database_path == tmp_path / "data" / "jarvis.sqlite3"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("JARVIS_ENVIRONMENT", "unsafe"),
        ("JARVIS_LOG_LEVEL", "VERBOSE"),
        ("JARVIS_LLM_MODEL", " "),
    ],
)
def test_invalid_settings_fail_fast(tmp_path: Path, key: str, value: str) -> None:
    with pytest.raises(ValueError):
        Settings.from_env(tmp_path / "missing.env", {key: value})
