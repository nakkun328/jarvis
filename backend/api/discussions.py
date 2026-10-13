"""Group discussion endpoints (docs/discussion.md). Mounted only when the feature is on.

``GET /api/discussions/config`` and ``POST /api/discussions``. Cancelling uses the existing
``POST /api/tasks/{id}/cancel``; the messages are read with the existing conversation messages
route. Errors are ``{"detail": "<FIXED_CODE>"}`` and never repeat anything the client sent: the
body is parsed here instead of by a model whose validation errors would echo the input.
"""

import asyncio
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
_PERSONA_FIELDS = frozenset({"name", "role", "stance", "checklist", "model", "color"})
_STATUS = {
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.DUPLICATE_NAME: 409,
    ErrorCode.LIMIT: 409,
    ErrorCode.STORE_UNAVAILABLE: 503,
    ErrorCode.STORE_CORRUPT: 503,
    ErrorCode.STORE_TOO_LARGE: 413,
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

    def guard_mutation(request: Request) -> None:
        if is_cross_origin_request(request.scope, trusted_proxy):
            raise HTTPException(status_code=403, detail="forbidden")
        if request.headers.get("x-jarvis-confirm") != "1":
            raise HTTPException(status_code=403, detail="confirm_header_required")

    def fail(error: DiscussionError) -> HTTPException:
        return HTTPException(status_code=_STATUS.get(error.code, 400), detail=error.code.value)

    @router.get("/api/discussions/personas")
    def list_personas() -> Any:
        try:
            return JSONResponse(service.personas(), headers=_NO_STORE)
        except Exception as error:
            _LOG.warning("discussion.personas_failed", extra={"error_type": type(error).__name__})
            raise HTTPException(status_code=503, detail=ErrorCode.STORE_UNAVAILABLE.value) from None

    @router.post("/api/discussions/personas", status_code=201)
    async def create_persona(request: Request) -> Any:
        guard_mutation(request)
        body = await _read_body(request, _PERSONA_FIELDS)
        try:
            saved = await asyncio.to_thread(service.create_persona, body)
        except DiscussionError as error:
            raise fail(error) from None
        return JSONResponse(saved, status_code=201, headers=_NO_STORE)

    @router.put("/api/discussions/personas/{name:path}")
    async def replace_persona(name: str, request: Request) -> Any:
        guard_mutation(request)
        body = await _read_body(request, _PERSONA_FIELDS)
        try:
            saved = await asyncio.to_thread(service.replace_persona, name, body)
        except DiscussionError as error:
            raise fail(error) from None
        return JSONResponse(saved, headers=_NO_STORE)

    @router.delete("/api/discussions/personas/{name:path}")
    async def delete_persona(name: str, request: Request) -> Any:
        guard_mutation(request)
        try:
            await asyncio.to_thread(service.delete_persona, name)
        except DiscussionError as error:
            raise fail(error) from None
        return JSONResponse({"deleted": True}, headers=_NO_STORE)

    @router.post("/api/discussions", status_code=201)
    async def start_discussion(request: Request) -> dict[str, str]:
        guard_mutation(request)
        body = await _read_body(request, _FIELDS)
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


async def _read_body(request: Request, fields: frozenset[str]) -> dict[str, Any]:
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
    if not isinstance(decoded, dict) or set(decoded) - fields:
        raise invalid
    return decoded
