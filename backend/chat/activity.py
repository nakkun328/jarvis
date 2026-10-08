"""Fixed-vocabulary activity events: which stage a chat turn is in right now.

An activity event says *what kind of step* is happening, never *what is in it*. Every field is
an enum value, a small count or a fixed code, so an event cannot carry the user's message, a
model reply, memory content, a path or upstream error text. The constructors below are the only
supported way to build one, and ``__post_init__`` rejects any field that does not belong to the
stage.

Emitted today by ``ChatService``: ``received``, ``memory_lookup`` (only when memory context is
configured and was consulted), ``generating``, ``done`` and ``error``.

In the vocabulary but NOT emitted yet, because the features do not exist: ``routing``,
``route_selected``, ``researching`` and ``speaking``. They are reserved so the later router,
Researcher and realtime work can reuse one vocabulary. Nothing may fake them.
"""

from dataclasses import dataclass
from enum import StrEnum

# A memory lookup returns at most three notes; the cap only keeps the field small and sane.
MAX_COUNT = 99


class ActivityStage(StrEnum):
    RECEIVED = "received"
    ROUTING = "routing"
    ROUTE_SELECTED = "route_selected"
    MEMORY_LOOKUP = "memory_lookup"
    RESEARCHING = "researching"
    GENERATING = "generating"
    SPEAKING = "speaking"
    DONE = "done"
    ERROR = "error"


class ActivityRoute(StrEnum):
    CASUAL = "casual"
    MEMORY = "memory"
    RESEARCH = "research"
    MAIN = "main"


class ResearchStep(StrEnum):
    PLANNING = "planning"
    SEARCHING = "searching"
    READING = "reading"
    VERIFYING = "verifying"
    WRITING = "writing"


class ActivityErrorCode(StrEnum):
    """Why a turn ended without a reply. Fixed codes; never an exception message."""

    CONVERSATION_NOT_FOUND = "conversation_not_found"
    CAPACITY = "capacity"
    STORAGE = "storage"
    MEMORY = "memory"
    PROVIDER = "provider"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


# The one field each stage may (or must) carry. Everything else is rejected.
_FIELD_BY_STAGE: dict[ActivityStage, str | None] = {
    ActivityStage.RECEIVED: None,
    ActivityStage.ROUTING: None,
    ActivityStage.ROUTE_SELECTED: "route",
    ActivityStage.MEMORY_LOOKUP: "count",
    ActivityStage.RESEARCHING: "step",
    ActivityStage.GENERATING: None,
    ActivityStage.SPEAKING: None,
    ActivityStage.DONE: None,
    ActivityStage.ERROR: "code",
}
_FIELD_TYPES: dict[str, type] = {
    "route": ActivityRoute,
    "count": int,
    "step": ResearchStep,
    "code": ActivityErrorCode,
}


@dataclass(frozen=True)
class ActivityEvent:
    stage: ActivityStage
    route: ActivityRoute | None = None
    count: int | None = None
    step: ResearchStep | None = None
    code: ActivityErrorCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, ActivityStage):
            raise ValueError("unknown activity stage")
        allowed = _FIELD_BY_STAGE[self.stage]
        for name in _FIELD_TYPES:
            value = getattr(self, name)
            if name != allowed:
                if value is not None:
                    raise ValueError(f"{self.stage.value} takes no {name}")
                continue
            if type(value) is not _FIELD_TYPES[name]:
                raise ValueError(f"{self.stage.value} requires a valid {name}")
        if self.count is not None and not 0 <= self.count <= MAX_COUNT:
            raise ValueError("count out of range")

    @classmethod
    def received(cls) -> "ActivityEvent":
        return cls(ActivityStage.RECEIVED)

    @classmethod
    def routing(cls) -> "ActivityEvent":
        return cls(ActivityStage.ROUTING)

    @classmethod
    def route_selected(cls, route: ActivityRoute) -> "ActivityEvent":
        return cls(ActivityStage.ROUTE_SELECTED, route=route)

    @classmethod
    def memory_lookup(cls, count: int) -> "ActivityEvent":
        return cls(ActivityStage.MEMORY_LOOKUP, count=count)

    @classmethod
    def researching(cls, step: ResearchStep) -> "ActivityEvent":
        return cls(ActivityStage.RESEARCHING, step=step)

    @classmethod
    def generating(cls) -> "ActivityEvent":
        return cls(ActivityStage.GENERATING)

    @classmethod
    def speaking(cls) -> "ActivityEvent":
        return cls(ActivityStage.SPEAKING)

    @classmethod
    def done(cls) -> "ActivityEvent":
        return cls(ActivityStage.DONE)

    @classmethod
    def error(cls, code: ActivityErrorCode) -> "ActivityEvent":
        return cls(ActivityStage.ERROR, code=code)

    def to_payload(self) -> dict[str, str | int]:
        """The SSE/JSON form: ``stage`` plus the stage's single allowlisted field, if any."""
        payload: dict[str, str | int] = {"stage": self.stage.value}
        field = _FIELD_BY_STAGE[self.stage]
        if field is not None:
            value = getattr(self, field)
            payload[field] = value.value if isinstance(value, StrEnum) else value
        return payload
