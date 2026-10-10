"""JARVIS_RESEARCH_ENABLED: off by default, strict when set."""

from pathlib import Path

import pytest

from backend.core.config import ConfigError, Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("JARVIS_RESEARCH_ENABLED", "JARVIS_SEARCH_PROVIDER", "JARVIS_SEARCH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "config.sqlite3"))


def test_research_is_off_by_default() -> None:
    assert Settings.from_env().research_enabled is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
def test_truthy_values_turn_it_on(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("JARVIS_RESEARCH_ENABLED", raw)
    assert Settings.from_env().research_enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_falsy_values_keep_it_off(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("JARVIS_RESEARCH_ENABLED", raw)
    assert Settings.from_env().research_enabled is False


@pytest.mark.parametrize("raw", ["", "2", "enabled", "tavily", "yes please"])
def test_an_invalid_value_is_a_config_error_that_does_not_echo_it(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("JARVIS_RESEARCH_ENABLED", raw)
    with pytest.raises(ConfigError) as error:
        Settings.from_env()
    assert "JARVIS_RESEARCH_ENABLED" in str(error.value)
    if raw.strip():
        assert raw not in str(error.value).replace("JARVIS_RESEARCH_ENABLED", "")


def test_a_non_boolean_in_code_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Settings(db_path=tmp_path / "x.sqlite3", research_enabled="1")  # type: ignore[arg-type]


def test_the_switch_alone_does_not_configure_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_RESEARCH_ENABLED", "1")
    settings = Settings.from_env()
    assert settings.research_enabled and settings.search_provider == "none"
