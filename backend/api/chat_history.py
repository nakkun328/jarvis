"""Read-only conversation history: the conversation list and the full stored transcript.

The database keeps every message of a conversation; only the last few are sent to the model.
These routes expose the stored data for display and never change it. The list carries a title
derived from the first user message, never message bodies. Errors use fixed codes.
"""

import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from backend.chat.persistence import ConversationStorageError
from backend.chat.service import ChatService

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100
DEFAULT_MESSAGES_LIMIT = 50
MAX_MESSAGES_LIMIT = 200

_NO_STORE = {"Cache-Control": "no-store"}
_INT = re.compile(r"[0-9]{1,9}")
_CURSOR = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z)_"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


class ConversationItem(BaseModel):
    id: UUID
    title: str
    updated_at: str
    message_count: int


class ConversationListResponse(BaseModel):
    conversations: list[ConversationItem]
    next_cursor: str | None


class HistoryMessage(BaseModel):
    role: str
    content: str


class HistoryResponse(BaseModel):
    conversation_id: UUID
    messages: list[HistoryMessage]
    has_more: bool
    next_before: str | None


def _single(request: Request, name: str) -> str | None:
    values = request.query_params.getlist(name)
    if len(values) > 1:
        raise HTTPException(status_code=422, detail=f"invalid_{name}")
    return values[0] if values else None


def _bounded(request: Request, name: str, default: int, maximum: int) -> int:
    raw = _single(request, name)
    if raw is None:
        return default
    if not _INT.fullmatch(raw) or not 1 <= int(raw) <= maximum:
        raise HTTPException(status_code=422, detail=f"invalid_{name}")
    return int(raw)


def _parse_conversation_id(raw: str) -> UUID:
    # Canonical lowercase hyphenated form only; anything else is the same 404 as an unknown id.
    try:
        conversation_id = UUID(raw)
    except ValueError:
        raise HTTPException(status_code=404, detail="conversation not found") from None
    if str(conversation_id) != raw:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conversation_id


def build_chat_history_router(service: ChatService | None) -> APIRouter:
    router = APIRouter()

    def require_service() -> ChatService:
        if service is None:
            raise HTTPException(status_code=503, detail="chat provider is not configured")
        return service

    @router.get("/api/chat/conversations", response_model=ConversationListResponse)
    async def list_conversations(request: Request, response: Response) -> ConversationListResponse:
        """Newest activity first. `before` is the opaque `next_cursor` of the previous page."""
        limit = _bounded(request, "limit", DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT)
        raw_cursor = _single(request, "before")
        before = None
        if raw_cursor is not None:
            match = _CURSOR.fullmatch(raw_cursor)
            if match is None:
                raise HTTPException(status_code=422, detail="invalid_before")
            before = (match.group(1), match.group(2))
        chat_service = require_service()
        try:
            page = await chat_service.store.list_conversations(limit=limit, before=before)
        except ConversationStorageError as exc:
            raise HTTPException(status_code=503, detail="conversation storage unavailable") from exc
        response.headers.update(_NO_STORE)
        last = page.items[-1] if page.items else None
        return ConversationListResponse(
            conversations=[
                ConversationItem(
                    id=item.id,
                    title=item.title,
                    updated_at=item.updated_at,
                    message_count=item.message_count,
                )
                for item in page.items
            ],
            next_cursor=f"{last.updated_at}_{last.id}" if last and page.has_more else None,
        )

    @router.get(
        "/api/chat/conversations/{conversation_id}/messages", response_model=HistoryResponse
    )
    async def conversation_messages(
        conversation_id: str, request: Request, response: Response
    ) -> HistoryResponse:
        """Read-only: the stored transcript, newest page first; `before` pages to older."""
        parsed = _parse_conversation_id(conversation_id)
        limit = _bounded(request, "limit", DEFAULT_MESSAGES_LIMIT, MAX_MESSAGES_LIMIT)
        raw_before = _single(request, "before")
        if raw_before is not None and (
            not _INT.fullmatch(raw_before) or int(raw_before) < 1
        ):
            raise HTTPException(status_code=422, detail="invalid_before")
        before = None if raw_before is None else int(raw_before)
        chat_service = require_service()
        try:
            page = await chat_service.store.messages_page(parsed, limit=limit, before=before)
        except ConversationStorageError as exc:
            raise HTTPException(status_code=503, detail="conversation storage unavailable") from exc
        if page is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        response.headers.update(_NO_STORE)
        return HistoryResponse(
            conversation_id=parsed,
            messages=[HistoryMessage(role=m.role, content=m.content) for m in page.messages],
            has_more=page.has_more,
            next_before=str(page.messages[0].id) if page.has_more and page.messages else None,
        )

    return router
