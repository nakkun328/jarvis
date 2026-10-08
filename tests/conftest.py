"""Shared test isolation."""

import logging
import socket
from collections.abc import Iterator

import pytest

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture(autouse=True)
def restore_logging_state() -> Iterator[None]:
    """Undo configure_logging() side effects so tests never leak handlers or levels."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    saved = {
        name: (list(logger.handlers), logger.propagate, logger.disabled, logger.level)
        for name in _UVICORN_LOGGERS
        for logger in (logging.getLogger(name),)
    }
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    for name, (kept, propagate, disabled, saved_level) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = kept
        logger.propagate, logger.disabled = propagate, disabled
        logger.setLevel(saved_level)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a connection or resolves a name (opt in per module)."""

    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
