"""Human approval endpoints for tool calls that need confirmation.

GET lists pending requests. POST approve/deny records the human's decision and nothing else: no
handler here runs, schedules or wakes a tool. The waiting executor picks the decision up from the
store on its own.

Unsafe requests are refused when the browser says they come from another origin, and also when
the fixed ``X-Jarvis-Confirm: 1`` header is missing (a cross-site page cannot add a custom header
without a CORS preflight, which this app never grants). With login enabled the auth middleware
additionally requires a session and a same-origin ``Origin``; these routes are not public.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.api.origin import is_cross_origin_request
from backend.tools.approvals import (
    ApprovalRequest,
    ApprovalStore,
    ApprovalStoreError,
    DecisionOutcome,
)

_LOG = logging.getLogger(__name__)

CONFIRM_HEADER = "x-jarvis-confirm"
ERROR_FORBIDDEN = "forbidden"
ERROR_HEADER_REQUIRED = "confirm_header_required"
ERROR_NOT_FOUND = "approval_not_found"
ERROR_NOT_PENDING = "approval_not_pending"
ERROR_EXPIRED = "approval_expired"
ERROR_STORAGE_UNAVAILABLE = "storage_unavailable"
_NO_STORE = {"Cache-Control": "no-store"}


def approval_json(request: ApprovalRequest) -> dict[str, Any]:
    return {
        "id": request.id,
        "tool_name": request.tool_name,
        "summary": request.summary,
        "requested_at": request.requested_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "expires_at": request.expires_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "state": request.state.value,
    }


def create_approvals_router(store: ApprovalStore, *, trusted_proxy: bool = False) -> APIRouter:
    router = APIRouter()

    def unavailable(exc: ApprovalStoreError) -> HTTPException:
        _LOG.warning("Approval storage failed: %s", type(exc).__name__)
        return HTTPException(status_code=503, detail=ERROR_STORAGE_UNAVAILABLE)

    def guard(request: Request) -> None:
        if is_cross_origin_request(request.scope, trusted_proxy):
            raise HTTPException(status_code=403, detail=ERROR_FORBIDDEN)
        if request.headers.get(CONFIRM_HEADER) != "1":
            raise HTTPException(status_code=403, detail=ERROR_HEADER_REQUIRED)

    def decide(approval_id: str, approve: bool) -> JSONResponse:
        try:
            outcome = store.decide(approval_id, approve)
        except ApprovalStoreError as exc:
            raise unavailable(exc) from exc
        if outcome is DecisionOutcome.APPLIED:
            state = "approved" if approve else "denied"
            return JSONResponse({"id": approval_id, "state": state}, headers=_NO_STORE)
        status, code = {
            DecisionOutcome.NOT_FOUND: (404, ERROR_NOT_FOUND),
            DecisionOutcome.NOT_PENDING: (409, ERROR_NOT_PENDING),
            DecisionOutcome.EXPIRED: (410, ERROR_EXPIRED),
        }[outcome]
        raise HTTPException(status_code=status, detail=code)

    @router.get("/api/approvals")
    def list_approvals() -> JSONResponse:
        try:
            pending = store.list_pending()
        except ApprovalStoreError as exc:
            raise unavailable(exc) from exc
        return JSONResponse({"approvals": [approval_json(a) for a in pending]}, headers=_NO_STORE)

    @router.post("/api/approvals/{approval_id}/approve")
    def approve(approval_id: str, request: Request) -> JSONResponse:
        guard(request)
        return decide(approval_id, True)

    @router.post("/api/approvals/{approval_id}/deny")
    def deny(approval_id: str, request: Request) -> JSONResponse:
        guard(request)
        return decide(approval_id, False)

    return router
