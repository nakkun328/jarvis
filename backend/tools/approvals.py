"""Human approvals for tool calls that need confirmation.

An approval request is a row in SQLite: tool name, the SHA-256 of the normalized arguments, and a
redacted, truncated summary for the human. Only `ApprovalStore.decide` can approve or deny, and
only the authenticated HTTP API calls it. The agent side gets `ApprovalRequester`, a view that can
ask for an approval, read its state and consume an approved one, but has no way to decide.

Rules enforced here (see docs/tool-confirmation.md):
- a decision is one-shot and bound to the exact tool name and argument digest;
- every request expires (default five minutes); an expired request can no longer be decided or used;
- `consume` is a single atomic UPDATE, so two racing executions cannot both use one approval;
- anything unclear (storage error, unknown id, bad state, timeout) means "not approved".
"""

import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from backend.core.database import Database
from backend.tools.contract import (
    ToolCall,
    canonical_json,
    digest_arguments,
    freeze_json,
    validate_tool_name,
)

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 300.0
MIN_TTL_SECONDS = 10.0
MAX_TTL_SECONDS = 3600.0
MAX_PENDING = 50
MAX_SUMMARY_FIELDS = 8
MAX_NAME_CHARS = 40
MAX_PREVIEW_CHARS = 80
REDACTED = "[redacted]"

_SECRET_NAME = re.compile(
    r"pass|secret|token|key|auth|cred|cookie|session|bearer|private|signature", re.IGNORECASE
)
_SECRET_VALUE = re.compile(
    r"(sk-|sk_|ghp_|gho_|github_pat_|xox[abp]-|AIza|AKIA|eyJ)[A-Za-z0-9_\-]{6,}"
    r"|bearer\s+\S+"
    r"|^[A-Za-z0-9+/=_\-]{32,}$",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class DecisionOutcome(StrEnum):
    """Result of a human decision attempt. Only APPLIED changed anything."""

    APPLIED = "applied"
    NOT_FOUND = "not_found"
    NOT_PENDING = "not_pending"
    EXPIRED = "expired"


class ApprovalStoreError(RuntimeError):
    """Approval storage failed. Callers treat this as "not approved"."""


class ApprovalQueueFull(ApprovalStoreError):
    """Too many requests are waiting for a human."""


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    tool_name: str
    summary: Mapping[str, Any]
    requested_at: datetime
    expires_at: datetime
    state: ApprovalState


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _clip(text: str, limit: int) -> str:
    cleaned = _CONTROL.sub(" ", text)
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _preview(name: str, value: Any) -> str:
    if _SECRET_NAME.search(name):
        return REDACTED
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, int | float):
        return _clip(json.dumps(value), MAX_PREVIEW_CHARS)
    if isinstance(value, str):
        if _SECRET_VALUE.search(value.strip()):
            return REDACTED
        return _clip(value, MAX_PREVIEW_CHARS)
    if isinstance(value, Mapping):
        return f"object({len(value)} keys)"
    try:
        return f"array({len(value)} items)"
    except TypeError:
        return "value"


def summarize_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Fixed-shape, bounded, redacted description of a call's arguments for a human.

    Only top-level names and short previews appear. Names that look like secrets and values that
    look like credentials are replaced; nested values show only their type and size. This is
    best-effort redaction: the digest, not the summary, is what binds an approval.
    """
    frozen = freeze_json(arguments)
    names = sorted(str(name) for name in frozen)
    fields = [
        {"name": _clip(name, MAX_NAME_CHARS), "preview": _preview(name, frozen[name])}
        for name in names[:MAX_SUMMARY_FIELDS]
    ]
    return {
        "fields": fields,
        "more_fields": max(0, len(names) - MAX_SUMMARY_FIELDS),
        "argument_bytes": len(canonical_json(frozen).encode("utf-8")),
        "digest_prefix": digest_arguments(frozen)[:12],
    }


class ApprovalStore:
    """SQLite-backed approval queue. Holds the only `decide` implementation."""

    def __init__(
        self,
        database: Database,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_pending: int = MAX_PENDING,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int | float)
            or not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS
        ):
            raise ValueError("ttl_seconds must be between 10 and 3600")
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        self.database = database
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_pending = max_pending
        self._clock = clock or (lambda: datetime.now(UTC))

    # ----- transactions -----

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            with self.database.connect() as connection:
                connection.isolation_level = None  # explicit BEGIN/COMMIT below
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                connection.execute("COMMIT")
        except sqlite3.Error as exc:
            logger.warning("approval storage failed: %s", type(exc).__name__)
            raise ApprovalStoreError("approval storage unavailable") from exc

    def _expire(self, connection: sqlite3.Connection, now: datetime) -> None:
        connection.execute(
            "UPDATE tool_approvals SET state = 'expired' "
            "WHERE state = 'pending' AND expires_at <= ?",
            (_stamp(now),),
        )

    # ----- agent-side operations (also exposed through `requester()`) -----

    def request(self, call: ToolCall) -> ApprovalRequest:
        """The pending request for this exact tool and arguments, created if there is none.

        Asking again for the same call returns the same request, so a looping model cannot
        flood the queue.
        """
        tool_name = validate_tool_name(call.tool_name)
        frozen = freeze_json(call.arguments)
        digest = digest_arguments(frozen)
        summary = json.dumps(summarize_arguments(frozen), sort_keys=True, ensure_ascii=False)
        now = self._clock()
        with self._tx() as connection:
            self._expire(connection, now)
            row = connection.execute(
                "SELECT * FROM tool_approvals WHERE state = 'pending' "
                "AND tool_name = ? AND args_digest = ?",
                (tool_name, digest),
            ).fetchone()
            if row is None:
                pending = connection.execute(
                    "SELECT COUNT(*) FROM tool_approvals WHERE state = 'pending'"
                ).fetchone()[0]
                if pending >= self._max_pending:
                    raise ApprovalQueueFull("too many pending approvals")
                approval_id = str(uuid4())
                connection.execute(
                    "INSERT INTO tool_approvals (id, tool_name, args_digest, summary, state, "
                    "requested_at, expires_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        approval_id,
                        tool_name,
                        digest,
                        summary,
                        _stamp(now),
                        _stamp(now + self._ttl),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM tool_approvals WHERE id = ?", (approval_id,)
                ).fetchone()
        return _to_request(row)

    def state_of(self, approval_id: str) -> ApprovalState | None:
        """Current state (expiry applied), or None for an unknown id."""
        now = self._clock()
        with self._tx() as connection:
            self._expire(connection, now)
            row = connection.execute(
                "SELECT state, expires_at, consumed_at FROM tool_approvals WHERE id = ?",
                (str(approval_id),),
            ).fetchone()
        if row is None:
            return None
        if row["state"] == "approved" and _parse(row["expires_at"]) <= now:
            return ApprovalState.EXPIRED
        return ApprovalState(row["state"])

    def consume(self, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        """Use up one approved, unexpired, unconsumed approval for exactly these arguments.

        One UPDATE selects and marks the row, so of any number of concurrent callers at most one
        sees True. Returns False on any doubt, including storage errors.
        """
        try:
            tool_name = validate_tool_name(tool_name)
            digest = digest_arguments(arguments)
            now = self._clock()
            with self._tx() as connection:
                cursor = connection.execute(
                    "UPDATE tool_approvals SET consumed_at = ? WHERE id = ("
                    "SELECT id FROM tool_approvals WHERE tool_name = ? AND args_digest = ? "
                    "AND state = 'approved' AND consumed_at IS NULL AND expires_at > ? "
                    "ORDER BY requested_at LIMIT 1)",
                    (_stamp(now), tool_name, digest, _stamp(now)),
                )
                return cursor.rowcount == 1
        except (ApprovalStoreError, ValueError):
            return False

    def requester(self) -> "ApprovalRequester":
        return ApprovalRequester(self)

    # ----- human-side operations (the API layer only) -----

    def list_pending(self) -> list[ApprovalRequest]:
        now = self._clock()
        with self._tx() as connection:
            self._expire(connection, now)
            rows = connection.execute(
                "SELECT * FROM tool_approvals WHERE state = 'pending' ORDER BY requested_at, id"
            ).fetchall()
        return [_to_request(row) for row in rows]

    def decide(self, approval_id: str, approve: bool) -> DecisionOutcome:
        """Record the human's decision on a pending, unexpired request, exactly once."""
        try:
            key = str(UUID(str(approval_id)))
        except ValueError:
            return DecisionOutcome.NOT_FOUND
        now = self._clock()
        with self._tx() as connection:
            self._expire(connection, now)
            cursor = connection.execute(
                "UPDATE tool_approvals SET state = ?, resolved_at = ? "
                "WHERE id = ? AND state = 'pending' AND expires_at > ?",
                ("approved" if approve else "denied", _stamp(now), key, _stamp(now)),
            )
            if cursor.rowcount == 1:
                return DecisionOutcome.APPLIED
            row = connection.execute(
                "SELECT state FROM tool_approvals WHERE id = ?", (key,)
            ).fetchone()
        if row is None:
            return DecisionOutcome.NOT_FOUND
        if row["state"] == "expired":
            return DecisionOutcome.EXPIRED
        return DecisionOutcome.NOT_PENDING


class ApprovalRequester:
    """What the executor holds: request, read state, consume. No way to decide."""

    def __init__(self, store: ApprovalStore) -> None:
        self._store = store

    def request(self, call: ToolCall) -> ApprovalRequest:
        return self._store.request(call)

    def state_of(self, approval_id: str) -> ApprovalState | None:
        return self._store.state_of(approval_id)

    def consume(self, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        return self._store.consume(tool_name, arguments)


def _to_request(row: sqlite3.Row) -> ApprovalRequest:
    return ApprovalRequest(
        id=row["id"],
        tool_name=row["tool_name"],
        summary=json.loads(row["summary"]),
        requested_at=_parse(row["requested_at"]),
        expires_at=_parse(row["expires_at"]),
        state=ApprovalState(row["state"]),
    )
