"""Stage verified research claims as pending memory candidates, on a human action only.

``POST /api/research/sessions/{id}/memory-candidates`` creates pending candidates for a completed
session's verified claims; ``GET`` on the same path lists the ones already staged. Nothing is
approved here and no vault note is written: a person approves a candidate later through the
normal memory review, which publishes through the memory writer.

The POST needs the same-origin check and the fixed ``X-Jarvis-Confirm: 1`` header (as the
approvals routes do) and is reachable only as an HTTP request from the page. No model, tool or
agent code calls it. Error bodies carry a fixed code and never repeat stored or sent text.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.api.approvals import CONFIRM_HEADER, ERROR_FORBIDDEN, ERROR_HEADER_REQUIRED
from backend.api.memory import memory_dto
from backend.api.origin import is_cross_origin_request
from backend.api.research import ERROR_STORAGE_UNAVAILABLE, _parse_session_id
from backend.memory.repository import MemoryRepositoryError
from backend.research.memory_candidates import (
    CandidateRefusal,
    CandidateSet,
    RefusedCandidates,
    ResearchMemoryCandidates,
)
from backend.research.repository import ResearchRepositoryError

_LOG = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "no-store"}

_REFUSAL_STATUS = {
    CandidateRefusal.SESSION_NOT_FOUND: 404,
    CandidateRefusal.SESSION_NOT_COMPLETED: 409,
    CandidateRefusal.NO_VERIFIED_CLAIMS: 409,
}


def _body(result: CandidateSet) -> dict[str, Any]:
    return {
        "eligible": result.eligible,
        "created": result.created,
        "omitted": result.omitted,
        "candidates": [memory_dto(stored) for stored in result.candidates],
    }


def create_research_memory_router(
    service: ResearchMemoryCandidates, *, trusted_proxy: bool = False
) -> APIRouter:
    router = APIRouter()

    def unavailable(exc: Exception) -> HTTPException:
        _LOG.warning("Research memory storage failed: %s", type(exc).__name__)
        return HTTPException(status_code=503, detail=ERROR_STORAGE_UNAVAILABLE)

    @router.get("/api/research/sessions/{session_id}/memory-candidates")
    def list_candidates(session_id: str) -> JSONResponse:
        parsed = _parse_session_id(session_id)
        try:
            result = service.existing(parsed)
        except RefusedCandidates as refusal:
            raise HTTPException(
                status_code=_REFUSAL_STATUS[refusal.code], detail=refusal.code.value
            ) from None
        except (ResearchRepositoryError, MemoryRepositoryError, ValueError, KeyError) as exc:
            raise unavailable(exc) from exc
        return JSONResponse(_body(result), headers=_NO_STORE)

    @router.post("/api/research/sessions/{session_id}/memory-candidates")
    def create_candidates(session_id: str, request: Request) -> JSONResponse:
        if is_cross_origin_request(request.scope, trusted_proxy):
            raise HTTPException(status_code=403, detail=ERROR_FORBIDDEN)
        if request.headers.get(CONFIRM_HEADER) != "1":
            raise HTTPException(status_code=403, detail=ERROR_HEADER_REQUIRED)
        parsed = _parse_session_id(session_id)
        try:
            result = service.stage(parsed)
        except RefusedCandidates as refusal:
            raise HTTPException(
                status_code=_REFUSAL_STATUS[refusal.code], detail=refusal.code.value
            ) from None
        except (ResearchRepositoryError, MemoryRepositoryError, ValueError, KeyError) as exc:
            raise unavailable(exc) from exc
        return JSONResponse(
            _body(result), status_code=201 if result.created else 200, headers=_NO_STORE
        )

    return router
