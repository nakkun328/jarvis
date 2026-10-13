"""Group discussion of several personas (docs/discussion.md). A library: not wired to the API."""

from backend.discussion.engine import (
    DiscussionEngine,
    DiscussionMessage,
    DiscussionResult,
    MessageKind,
    StopCode,
)
from backend.discussion.errors import DiscussionError
from backend.discussion.personas import (
    Persona,
    load_personas,
    parse_personas,
    preset_personas,
)

__all__ = [
    "DiscussionEngine",
    "DiscussionError",
    "DiscussionMessage",
    "DiscussionResult",
    "MessageKind",
    "Persona",
    "StopCode",
    "load_personas",
    "parse_personas",
    "preset_personas",
]
