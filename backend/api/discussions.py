"""Group discussion endpoints (docs/discussion.md). Mounted only when the feature is on.

``GET /api/discussions/config`` and ``POST /api/discussions``. Cancelling uses the existing
``POST /api/tasks/{id}/cancel``; the messages are read with the existing conversation messages
route. Errors are ``{"detail": "<FIXED_CODE>"}`` and never repeat anything the client sent: the
body is parsed here instead of by a model whose validation errors would echo the input.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.api.origin import is_cross_origin_request
from backend.discussion.errors import DiscussionError, ErrorCode
from backend.discussion.service import DiscussionService

_LOG = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
_FIELDS = frozenset({"topic", "personas", "rounds"})
_STATUS = {
    ErrorCode.BUSY: 409,
    ErrorCode.DAILY_LIMIT: 429,
    ErrorCode.MODEL_UNAVAILABLE: 503,
    ErrorCode.STORAGE_UNAVAILABLE: 503,
}
_NO_STORE = {"Cache-Control": "no-store"}


def create_discussions_router(
    service: DiscussionService, *, trusted_proxy: bool = False
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/discussions/config")
    def discussion_config() -> Any:
        try:
            return JSONResponse(service.config(), headers=_NO_STORE)
        except Exception as error:
            _LOG.warning("discussion.config_failed", extra={"error_type": type(error).__name__})
            raise HTTPException(
                status_code=503, detail=ErrorCode.STORAGE_UNAVAILABLE.value
            ) from None

    @router.post("/api/discussions", status_code=201)
    async def start_discussion(request: Request) -> dict[str, str]:
        if is_cross_origin_request(request.scope, trusted_proxy):
            raise HTTPException(status_code=403, detail="forbidden")
        if request.headers.get("x-jarvis-confirm") != "1":
            raise HTTPException(status_code=403, detail="confirm_header_required")
        body = await _read_body(request)
        try:
            conversation_id, task_id = await service.start(
                body.get("topic"), body.get("personas"), body.get("rounds")
            )
        except DiscussionError as error:
            raise HTTPException(
                status_code=_STATUS.get(error.code, 400), detail=error.code.value
            ) from None
        return {"conversation_id": str(conversation_id), "task_id": str(task_id)}

    return router


async def _read_body(request: Request) -> dict[str, Any]:
    invalid = HTTPException(status_code=400, detail=ErrorCode.REQUEST_INVALID.value)
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        raise invalid
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BODY_BYTES:
            raise invalid
    try:
        decoded = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise invalid from None
    if not isinstance(decoded, dict) or set(decoded) - _FIELDS:
        raise invalid
    return decoded
