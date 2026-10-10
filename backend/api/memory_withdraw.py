"""Human-only withdrawal of an automatically approved research or chat memory (docs/memory.md).

``POST /api/memory/notes/{id}/withdraw`` retires one approved note whose approval was recorded as
automatic by the actor of its own origin (``research`` or ``chat``). It reuses the existing
retirement transition: the record becomes ``retired`` (so it no longer reaches a model) and a
lifecycle event records the withdrawal. Nothing is deleted: the vault note, if any, stays on disk
for inspection and the history stays in SQLite.

The request needs the same-origin check and the fixed ``X-Jarvis-Confirm: 1`` header (as the
approvals routes do), and the login layer when it is on. No model, tool or agent code calls it.
Notes that a person approved, or that came from anywhere other than research or chat, are refused.
Error bodies carry a fixed code and never repeat stored or sent text.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.api.approvals import CONFIRM_HEADER, ERROR_FORBIDDEN, ERROR_HEADER_REQUIRED
from backend.api.memory import (
    ERROR_NOT_FOUND,
    ERROR_STORAGE_UNAVAILABLE,
    _parse_memory_id,
    memory_dto,
    with_auto_flag,
)
from backend.api.origin import is_cross_origin_request
from backend.memory.auto_approval import (
    AUTO_ACTORS,
    WITHDRAW_ACTOR,
    WITHDRAW_REASON,
    WITHDRAW_REASON_CHAT,
    is_auto_approved,
)
from backend.memory.events import (
    MemoryEventKind,
    MemoryEventOrigin,
    MemoryEventPublisher,
    safe_publish,
)
from backend.memory.model import MemoryOrigin
from backend.memory.repository import (
    MemoryRepository,
    MemoryRepositoryError,
    MemoryStateChanged,
    MemoryStatus,
)
from backend.memory.writer import MemoryWriteError, MemoryWriter

_LOG = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "no-store"}
ERROR_NOT_AUTO_APPROVED = "not_auto_approved"
ERROR_STATE_CHANGED = "state_changed"


def create_memory_withdraw_router(
    repository: MemoryRepository,
    writer: MemoryWriter | None = None,
    *,
    trusted_proxy: bool = False,
    publisher: MemoryEventPublisher | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/memory/notes/{memory_id}/withdraw")
    def withdraw(memory_id: str, request: Request) -> JSONResponse:
        if is_cross_origin_request(request.scope, trusted_proxy):
            raise HTTPException(status_code=403, detail=ERROR_FORBIDDEN)
        if request.headers.get(CONFIRM_HEADER) != "1":
            raise HTTPException(status_code=403, detail=ERROR_HEADER_REQUIRED)
        parsed = _parse_memory_id(memory_id)
        try:
            stored = repository.get(parsed)
            if stored is None:
                raise HTTPException(status_code=404, detail=ERROR_NOT_FOUND)
            if (
                stored.status is not MemoryStatus.APPROVED
                or stored.record.origin not in AUTO_ACTORS
                or not is_auto_approved(repository, parsed, stored.record.origin)
            ):
                raise HTTPException(status_code=409, detail=ERROR_NOT_AUTO_APPROVED)
            reason = (
                WITHDRAW_REASON_CHAT
                if stored.record.origin is MemoryOrigin.CHAT
                else WITHDRAW_REASON
            )
            retired = None
            if writer is not None:
                try:
                    retired = writer.retire(parsed, actor=WITHDRAW_ACTOR, reason=reason)
                except MemoryWriteError as exc:
                    # The vault note was edited, is missing or unreadable. Withdrawing must still
                    # work: retire the record against the revision recorded at approval.
                    _LOG.info("Withdrawal used the recorded revision: %s", type(exc).__name__)
            if retired is None:
                retired = repository.retire(
                    parsed,
                    vault_revision=stored.vault_revision or "unknown",
                    actor=WITHDRAW_ACTOR,
                    reason=reason,
                )
        except HTTPException:
            raise
        except MemoryStateChanged:
            raise HTTPException(status_code=409, detail=ERROR_STATE_CHANGED) from None
        except (MemoryRepositoryError, ValueError, KeyError) as exc:
            _LOG.warning("Memory storage failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ERROR_STORAGE_UNAVAILABLE) from exc
        safe_publish(
            publisher,
            MemoryEventKind.WITHDRAWN,
            MemoryEventOrigin.CHAT
            if stored.record.origin is MemoryOrigin.CHAT
            else MemoryEventOrigin.RESEARCH,
            parsed,
            stored.record.content,
        )
        body: dict[str, Any] = with_auto_flag(memory_dto(retired), repository, retired)
        return JSONResponse(body, headers=_NO_STORE)

    return router
