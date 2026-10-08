"""Conversation router contract (library only; not wired into chat)."""

from backend.router.audit import (
    AuditedRouter,
    InMemoryAuditSink,
    RouteAuditRecord,
    RouteAuditSink,
)
from backend.router.contract import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    FALLBACK_REASONS,
    Route,
    RouteDecision,
    Router,
    RouterContractError,
    RouteReason,
    fallback,
)
from backend.router.llm import LLMRouter
from backend.router.rule import RuleRouter

__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "FALLBACK_REASONS",
    "AuditedRouter",
    "InMemoryAuditSink",
    "LLMRouter",
    "Route",
    "RouteAuditRecord",
    "RouteAuditSink",
    "RouteDecision",
    "RouteReason",
    "Router",
    "RouterContractError",
    "RuleRouter",
    "fallback",
]
