"""Fixed-vocabulary activity events: which stage a chat turn is in right now.

An activity event says *what kind of step* is happening, never *what is in it*. Every field is
an enum value, a small count or a fixed code, so an event cannot carry the user's message, a
model reply, memory content, a path or upstream error text. The constructors below are the only
supported way to build one, and ``__post_init__`` rejects any field that does not belong to the
stage.

Emitted today by ``ChatService``: ``received``, ``routing`` and ``route_selected`` (only when a
router was explicitly configured), ``memory_lookup`` (only when memory context is configured and
was consulted), ``researching`` (once, with step ``started``, only when the router chose research
and a research really was started), ``generating``, ``done`` and ``error``.

``route_selected`` says two different things and never mixes them up: ``route`` is the path that
actually runs and ``decided`` is what the router chose. ``route`` is ``main`` unless a research was
really started for the turn (then ``research``) or the casual path really answered it (then
``casual``, only with ``JARVIS_CASUAL`` on and never after a router fallback). ``fallback`` says the
router could not decide and the safe default was used. When the router chose research but none was
started, the optional ``research_skip`` carries the fixed reason (research busy, not available, over
budget, refused, or too uncertain) and the turn runs on ``main``. Likewise ``casual_skip`` (over
the daily cap, provider failure before the first token, or too uncertain), present only with the
casual path on. A casual turn emits no ``memory_lookup``.

In the vocabulary but NOT emitted yet, because the feature does not exist: ``speaking``, and the
``researching`` steps after ``started`` (the chat does not follow a research run; the Research
screen does). They are reserved so the later realtime work can reuse one vocabulary. Nothing may
fake them.
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
    # The only step the chat emits: a research was handed to the research run service.
    STARTED = "started"
    PLANNING = "planning"
    SEARCHING = "searching"
    READING = "reading"
    VERIFYING = "verifying"
    WRITING = "writing"


class ResearchSkip(StrEnum):
    """Why a research the router asked for was not started. Fixed codes; never a message."""

    BUSY = "busy"
    NOT_CONFIGURED = "not_configured"
    BUDGET_EXHAUSTED = "budget_exhausted"
    REFUSED = "refused"
    LOW_CONFIDENCE = "low_confidence"


class CasualSkip(StrEnum):
    """Why a turn the router decided as casual ran on the Main Agent. Fixed codes. Only present
    when the casual path is switched on (``JARVIS_CASUAL``)."""

    OVER_BUDGET = "over_budget"
    PROVIDER = "provider"
    LOW_CONFIDENCE = "low_confidence"


class ActivityErrorCode(StrEnum):
    """Why a turn ended without a reply. Fixed codes; never an exception message."""

    CONVERSATION_NOT_FOUND = "conversation_not_found"
    CAPACITY = "capacity"
    STORAGE = "storage"
    MEMORY = "memory"
    PROVIDER = "provider"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


# What the router may have chosen. ``main`` is where a turn runs, never a router choice.
DECIDED_ROUTES = frozenset({ActivityRoute.CASUAL, ActivityRoute.MEMORY, ActivityRoute.RESEARCH})

# The fields each stage may (and must) carry. Everything else is rejected.
_FIELDS_BY_STAGE: dict[ActivityStage, tuple[str, ...]] = {
    ActivityStage.RECEIVED: (),
    ActivityStage.ROUTING: (),
    ActivityStage.ROUTE_SELECTED: ("route", "decided", "fallback"),
    ActivityStage.MEMORY_LOOKUP: ("count",),
    ActivityStage.RESEARCHING: ("step",),
    ActivityStage.GENERATING: (),
    ActivityStage.SPEAKING: (),
    ActivityStage.DONE: (),
    ActivityStage.ERROR: ("code",),
}
# Fields a stage may carry but need not. ``to_payload`` leaves them out when unset.
_OPTIONAL_FIELDS_BY_STAGE: dict[ActivityStage, tuple[str, ...]] = {
    ActivityStage.ROUTE_SELECTED: ("research_skip", "casual_skip"),
}
_FIELD_TYPES: dict[str, type] = {
    "route": ActivityRoute,
    "decided": ActivityRoute,
    "fallback": bool,
    "research_skip": ResearchSkip,
    "casual_skip": CasualSkip,
    "count": int,
    "step": ResearchStep,
    "code": ActivityErrorCode,
}


@dataclass(frozen=True)
class ActivityEvent:
    stage: ActivityStage
    route: ActivityRoute | None = None
    decided: ActivityRoute | None = None
    fallback: bool | None = None
    research_skip: ResearchSkip | None = None
    casual_skip: CasualSkip | None = None
    count: int | None = None
    step: ResearchStep | None = None
    code: ActivityErrorCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, ActivityStage):
            raise ValueError("unknown activity stage")
        allowed = _FIELDS_BY_STAGE[self.stage]
        optional = _OPTIONAL_FIELDS_BY_STAGE.get(self.stage, ())
        for name in _FIELD_TYPES:
            value = getattr(self, name)
            if name in optional:
                if value is not None and type(value) is not _FIELD_TYPES[name]:
                    raise ValueError(f"{self.stage.value} requires a valid {name}")
                continue
            if name not in allowed:
                if value is not None:
                    raise ValueError(f"{self.stage.value} takes no {name}")
                continue
            if type(value) is not _FIELD_TYPES[name]:
                raise ValueError(f"{self.stage.value} requires a valid {name}")
        if self.count is not None and not 0 <= self.count <= MAX_COUNT:
            raise ValueError("count out of range")
        if self.decided is not None and self.decided not in DECIDED_ROUTES:
            raise ValueError("decided must be a router choice")
        if self.stage is ActivityStage.ROUTE_SELECTED:
            # The path that ran must be one the router could have asked for: a research ran only
            # because the router decided it, and a skipped research is a turn that ran on main.
            if self.route is ActivityRoute.RESEARCH and (
                self.decided is not ActivityRoute.RESEARCH or self.fallback
            ):
                raise ValueError("research ran without a research decision")
            if self.research_skip is not None and (
                self.route is not ActivityRoute.MAIN
                or self.decided is not ActivityRoute.RESEARCH
                or self.fallback
            ):
                raise ValueError("research_skip needs a research decision that ran on main")
            if self.route is ActivityRoute.CASUAL and (
                self.decided is not ActivityRoute.CASUAL or self.fallback
            ):
                raise ValueError("casual ran without a casual decision")
            if self.casual_skip is not None and (
                self.route is not ActivityRoute.MAIN
                or self.decided is not ActivityRoute.CASUAL
                or self.fallback
            ):
                raise ValueError("casual_skip needs a casual decision that ran on main")

    @classmethod
    def received(cls) -> "ActivityEvent":
        return cls(ActivityStage.RECEIVED)

    @classmethod
    def routing(cls) -> "ActivityEvent":
        return cls(ActivityStage.ROUTING)

    @classmethod
    def route_selected(
        cls,
        route: ActivityRoute,
        decided: ActivityRoute,
        fallback: bool,
        research_skip: ResearchSkip | None = None,
        casual_skip: CasualSkip | None = None,
    ) -> "ActivityEvent":
        """``route``: the path that actually runs. ``decided``: what the router chose.

        ``research_skip``: why a research the router asked for was not started (the turn ran
        on ``main``). ``casual_skip``: the same for a casual decision, only with the casual path
        switched on.
        """
        return cls(
            ActivityStage.ROUTE_SELECTED,
            route=route,
            decided=decided,
            fallback=fallback,
            research_skip=research_skip,
            casual_skip=casual_skip,
        )

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

    def to_payload(self) -> dict[str, str | int | bool]:
        """The SSE/JSON form: ``stage`` plus the stage's allowlisted fields, if any."""
        payload: dict[str, str | int | bool] = {"stage": self.stage.value}
        optional = _OPTIONAL_FIELDS_BY_STAGE.get(self.stage, ())
        for field in (*_FIELDS_BY_STAGE[self.stage], *optional):
            value = getattr(self, field)
            if value is None and field in optional:
                continue
            payload[field] = value.value if isinstance(value, StrEnum) else value
        return payload
