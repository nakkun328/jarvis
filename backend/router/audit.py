"""Route audit: what was decided, never the text it was decided on."""

import hashlib
import logging
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from backend.router.contract import (
    Route,
    RouteDecision,
    Router,
    RouterContractError,
    RouteReason,
)

_LOG = logging.getLogger(__name__)

LENGTH_BUCKETS = ("0", "1-19", "20-99", "100-499", "500-1999", "2000+")
EVENT_DECISION = "router.decision"
EVENT_AUDIT_FAILED = "router.audit_failed"


def confidence_bucket(confidence: float) -> int:
    """Tenths: 0 for [0, 0.1), ... 9 for [0.9, 1.0), 10 only for exactly 1.0."""
    return min(10, max(0, int(confidence * 10)))


def length_bucket(length: int) -> str:
    for label, upper in zip(LENGTH_BUCKETS, (1, 20, 100, 500, 2000), strict=False):
        if length < upper:
            return label
    return LENGTH_BUCKETS[-1]


def input_digest(text: object) -> str:
    """SHA-256 of the input. Equal inputs share a digest, so treat it as pseudonymous."""
    data = text.encode("utf-8", "replace") if isinstance(text, str) else b""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class RouteAuditRecord:
    route: Route
    reason: RouteReason
    used_fallback: bool
    confidence_bucket: int
    length_bucket: str
    input_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.route, Route)
            or not isinstance(self.reason, RouteReason)
            or not isinstance(self.used_fallback, bool)
            or isinstance(self.confidence_bucket, bool)
            or not isinstance(self.confidence_bucket, int)
            or not 0 <= self.confidence_bucket <= 10
            or self.length_bucket not in LENGTH_BUCKETS
            or not isinstance(self.input_sha256, str)
            or len(self.input_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.input_sha256)
        ):
            raise RouterContractError("invalid audit record")

    @classmethod
    def from_decision(cls, text: object, decision: RouteDecision) -> "RouteAuditRecord":
        length = len(text) if isinstance(text, str) else 0
        return cls(
            decision.route,
            decision.reason,
            decision.used_fallback,
            confidence_bucket(decision.confidence),
            length_bucket(length),
            input_digest(text),
        )


class RouteAuditSink(Protocol):
    def record(self, record: RouteAuditRecord) -> None: ...


class InMemoryAuditSink:
    """Bounded in-memory sink for tests and local diagnostics."""

    def __init__(self, maximum: int = 1000) -> None:
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise ValueError("maximum must be a positive integer")
        self._records: deque[RouteAuditRecord] = deque(maxlen=maximum)

    def record(self, record: RouteAuditRecord) -> None:
        self._records.append(record)

    @property
    def records(self) -> tuple[RouteAuditRecord, ...]:
        return tuple(self._records)


class AuditedRouter:
    """Wraps a router and audits each decision. Audit failure never changes routing."""

    def __init__(self, router: Router, sink: RouteAuditSink) -> None:
        self._router = router
        self._sink = sink

    async def decide(self, text: str) -> RouteDecision:
        decision = await self._router.decide(text)
        try:
            record = RouteAuditRecord.from_decision(text, decision)
            self._sink.record(record)
            _LOG.info(
                EVENT_DECISION,
                extra={
                    "route": record.route.value,
                    "reason": record.reason.value,
                    "used_fallback": record.used_fallback,
                    "confidence_bucket": record.confidence_bucket,
                    "length_bucket": record.length_bucket,
                },
            )
        except Exception as exc:
            _LOG.warning(EVENT_AUDIT_FAILED, extra={"error_type": type(exc).__name__})
        return decision
