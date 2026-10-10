"""Backend-enforced tool permissions.

The policy is data supplied by the application. Model output, voice transcripts, and chat text
are never a permission source; only a `ConfirmationGrant` object created by application code
can confirm a Red (or non-allow-listed Yellow) call.

Not implemented: who the confirming human is. A grant records no actor identity and nothing here
authenticates one; whatever UI creates grants is responsible for that.
"""

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hmac import compare_digest
from types import MappingProxyType
from typing import Any

from backend.tools.contract import (
    PermissionLevel,
    ToolCall,
    ToolSpec,
    digest_arguments,
    validate_tool_name,
)

MAX_CONSUMED_GRANTS = 10_000

ScopeCheck = Callable[[ToolSpec, Mapping[str, Any]], bool]


class PermissionReason(StrEnum):
    GREEN_DEFAULT = "green_default"
    POLICY_ALLOWED = "policy_allowed"
    GRANT_ACCEPTED = "grant_accepted"
    UNKNOWN_TOOL = "unknown_tool"
    EXPLICIT_DENY = "explicit_deny"
    OUT_OF_SCOPE = "out_of_scope"
    SCOPE_CHECK_ERROR = "scope_check_error"
    CONFIRMATION_REQUIRED = "confirmation_required"
    GRANT_TOOL_MISMATCH = "grant_tool_mismatch"
    GRANT_CALL_MISMATCH = "grant_call_mismatch"
    GRANT_ARGUMENTS_MISMATCH = "grant_arguments_mismatch"
    GRANT_EXPIRED = "grant_expired"
    GRANT_REPLAYED = "grant_replayed"


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason_code: PermissionReason
    requires_confirmation: bool = False

    def __post_init__(self) -> None:
        if self.allowed and self.requires_confirmation:
            raise ValueError("an allowed decision cannot require confirmation")


@dataclass(frozen=True)
class ConfirmationGrant:
    """One-time approval bound to the exact tool, call, and arguments digest."""

    tool_name: str
    call_id: str
    argument_digest: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")

    @classmethod
    def for_call(cls, call: ToolCall, expires_at: datetime) -> "ConfirmationGrant":
        """Bind a grant to `call`. Call this only from application code after a human decision."""
        return cls(call.tool_name, call.call_id, digest_arguments(call.arguments), expires_at)


class _GrantLedger:
    """Remembers consumed grants until they would have expired anyway."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consumed: dict[tuple[str, str, str], datetime] = {}

    def consume(self, key: tuple[str, str, str], expires_at: datetime, now: datetime) -> bool:
        with self._lock:
            for old in [k for k, expiry in self._consumed.items() if expiry <= now]:
                del self._consumed[old]
            if key in self._consumed or len(self._consumed) >= MAX_CONSUMED_GRANTS:
                return False
            self._consumed[key] = expires_at
            return True


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _names(values: Iterable[str]) -> frozenset[str]:
    return frozenset(validate_tool_name(v) for v in values)


@dataclass(frozen=True)
class PermissionPolicy:
    """Allow/deny data plus optional per-tool scope predicates.

    Evaluation order: unknown tool, deny list, scope predicate, then the level rules.
    The deny list and a failed scope check beat every grant.
    """

    deny: frozenset[str] = frozenset()
    allow_yellow: frozenset[str] = frozenset()
    scope_checks: Mapping[str, ScopeCheck] = field(default_factory=dict, compare=False, hash=False)
    clock: Callable[[], datetime] = field(default=_utcnow, compare=False, hash=False, repr=False)
    _ledger: _GrantLedger = field(
        default_factory=_GrantLedger, init=False, compare=False, hash=False, repr=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "deny", _names(self.deny))
        object.__setattr__(self, "allow_yellow", _names(self.allow_yellow))
        checks = {validate_tool_name(k): v for k, v in self.scope_checks.items()}
        if not all(callable(v) for v in checks.values()):
            raise ValueError("scope checks must be callables")
        object.__setattr__(self, "scope_checks", MappingProxyType(checks))

    def evaluate(
        self,
        call: ToolCall,
        spec: ToolSpec | None,
        *,
        grant: ConfirmationGrant | None = None,
    ) -> PermissionDecision:
        """Decide for one call. `call.arguments` must already be validated by the caller.

        Accepting a grant consumes it, so evaluate once per execution attempt.
        """
        if spec is None:
            return _deny(PermissionReason.UNKNOWN_TOOL)
        if spec.name in self.deny or call.tool_name in self.deny:
            return _deny(PermissionReason.EXPLICIT_DENY)
        check = self.scope_checks.get(spec.name)
        if check is not None:
            try:
                in_scope = check(spec, call.arguments) is True
            except Exception:
                return _deny(PermissionReason.SCOPE_CHECK_ERROR)
            if not in_scope:
                return _deny(PermissionReason.OUT_OF_SCOPE)
        if spec.permission is PermissionLevel.GREEN:
            return PermissionDecision(True, PermissionReason.GREEN_DEFAULT)
        if spec.permission is PermissionLevel.YELLOW and spec.name in self.allow_yellow:
            return PermissionDecision(True, PermissionReason.POLICY_ALLOWED)
        return self._check_grant(call, spec, grant)

    def _check_grant(
        self, call: ToolCall, spec: ToolSpec, grant: ConfirmationGrant | None
    ) -> PermissionDecision:
        if grant is None:
            return _confirm(PermissionReason.CONFIRMATION_REQUIRED)
        if grant.tool_name != spec.name:
            return _confirm(PermissionReason.GRANT_TOOL_MISMATCH)
        if grant.call_id != call.call_id:
            return _confirm(PermissionReason.GRANT_CALL_MISMATCH)
        try:
            digest = digest_arguments(call.arguments)
        except ValueError:
            return _confirm(PermissionReason.GRANT_ARGUMENTS_MISMATCH)
        if not compare_digest(grant.argument_digest.encode(), digest.encode()):
            return _confirm(PermissionReason.GRANT_ARGUMENTS_MISMATCH)
        now = self.clock()
        if grant.expires_at <= now:
            return _confirm(PermissionReason.GRANT_EXPIRED)
        if not self._ledger.consume((call.call_id, spec.name, digest), grant.expires_at, now):
            return _confirm(PermissionReason.GRANT_REPLAYED)
        return PermissionDecision(True, PermissionReason.GRANT_ACCEPTED)


def _deny(reason: PermissionReason) -> PermissionDecision:
    return PermissionDecision(False, reason)


def _confirm(reason: PermissionReason) -> PermissionDecision:
    return PermissionDecision(False, reason, requires_confirmation=True)
