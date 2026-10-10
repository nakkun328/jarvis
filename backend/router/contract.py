"""Router contract: a validated, fixed-vocabulary routing decision.

A router only *chooses* a route. It never calls a tool, reads memory or writes
anything, and it never raises: every failure becomes the safe fallback (the Main
Agent path, ``Route.memory``) with a fixed reason code.
"""

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

# Proposal only; the maintainer owns the real number (docs/router.md).
DEFAULT_CONFIDENCE_THRESHOLD = 0.6
DEFAULT_MAX_INPUT_CHARS = 2000


class RouterContractError(ValueError):
    """A value violates the router contract."""


class Route(StrEnum):
    casual = "casual"
    # The existing Main Agent path with memory context; also the safe fallback.
    memory = "memory"
    research = "research"


class RouteReason(StrEnum):
    # Not fallbacks.
    model_choice = "model_choice"
    rule_match = "rule_match"
    # Fallbacks (see FALLBACK_REASONS).
    low_confidence = "low_confidence"
    invalid_output = "invalid_output"
    model_error = "model_error"
    timeout = "timeout"
    empty_input = "empty_input"
    input_too_long = "input_too_long"
    no_match = "no_match"
    no_model = "no_model"


FALLBACK_REASONS = frozenset(
    {
        RouteReason.low_confidence,
        RouteReason.invalid_output,
        RouteReason.model_error,
        RouteReason.timeout,
        RouteReason.empty_input,
        RouteReason.input_too_long,
        RouteReason.no_match,
        RouteReason.no_model,
    }
)
SAFE_ROUTE = Route.memory


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: Route
    confidence: float
    reason: RouteReason
    used_fallback: bool

    def __post_init__(self) -> None:
        if not isinstance(self.route, Route) or not isinstance(self.reason, RouteReason):
            raise RouterContractError("route and reason must be enum members")
        confidence = self.confidence
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise RouterContractError("confidence must be a number")
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise RouterContractError("confidence must be within [0, 1]")
        object.__setattr__(self, "confidence", float(confidence))
        if not isinstance(self.used_fallback, bool):
            raise RouterContractError("used_fallback must be a bool")
        if self.used_fallback != (self.reason in FALLBACK_REASONS):
            raise RouterContractError("used_fallback must match the reason code")
        if self.used_fallback and self.route is not SAFE_ROUTE:
            raise RouterContractError("a fallback decision must use the safe route")


@runtime_checkable
class Router(Protocol):
    async def decide(self, text: str) -> RouteDecision:
        """Choose a route for one user turn. Never raises (except cancellation)."""
        ...


def fallback(reason: RouteReason, confidence: float = 0.0) -> RouteDecision:
    """The safe decision: the Main Agent path with a fixed reason code."""
    return RouteDecision(SAFE_ROUTE, confidence, reason, True)


def validate_threshold(threshold: float) -> float:
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, int | float)
        or not math.isfinite(threshold)
        or not 0.0 <= threshold <= 1.0
    ):
        raise RouterContractError("threshold must be within [0, 1]")
    return float(threshold)


def validate_max_chars(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100_000:
        raise RouterContractError("max input length must be an integer from 1 to 100000")
    return value


def precheck_input(text: object, max_chars: int) -> RouteDecision | None:
    """A fallback for input a router must not process, else ``None``."""
    if not isinstance(text, str) or not text.strip():
        return fallback(RouteReason.empty_input)
    if len(text) > max_chars:
        # Rejected, not truncated: a clipped turn could change meaning and an
        # attacker could pad to hide the real request. Memory is the safe path.
        return fallback(RouteReason.input_too_long)
    return None


def decide_with_threshold(route: Route, confidence: float, threshold: float) -> RouteDecision:
    """Apply the confidence policy to a model-chosen route."""
    if confidence < threshold:
        return fallback(RouteReason.low_confidence, confidence)
    return RouteDecision(route, confidence, RouteReason.model_choice, False)
