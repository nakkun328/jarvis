"""Chat HTTP and SSE endpoints."""

import json
from contextlib import aclosing
from typing import Annotated
from uuid import UUID

from anyio import CancelScope
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send

from backend.chat.activity import ActivityEvent
from backend.chat.context import ConversationCapacityError, ConversationNotFound
from backend.chat.memory_context import MemoryContextError
from backend.chat.persistence import ConversationStorageError
from backend.chat.service import ChatDelta, ChatService
from backend.providers.base import ProviderError
from backend.providers.choices import ModelUnavailable, UnknownModelChoice


class _ClosingStreamingResponse(StreamingResponse):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A failed/cancelled send can leave the body suspended at yield.
            with CancelScope(shield=True):
                await self.body_iterator.aclose()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    # Exactly one entry of the server's JARVIS_MODEL_CHOICES allowlist, or absent for the
    # default model. Validated strictly in ``check_model``; never forwarded as given.
    model_choice: str | None = Field(default=None, max_length=100)


class ChatResponse(BaseModel):
    conversation_id: UUID
    reply: str
    provider: str
    model: str


# A client opts in to `activity` SSE events by sending this header with the value "1". Without
# it the stream is exactly the delta/done/error stream it always was.
ACTIVITY_HEADER = "X-Jarvis-Activity"


def _sse(event: str, data: dict[str, str | int | bool]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class HistoryMessage(BaseModel):
    role: str
    content: str


class HistoryResponse(BaseModel):
    conversation_id: UUID
    messages: list[HistoryMessage]


def _parse_conversation_id(raw: str) -> UUID:
    # Canonical lowercase hyphenated form only; anything else is the same 404 as an unknown id.
    try:
        conversation_id = UUID(raw)
    except ValueError:
        raise HTTPException(status_code=404, detail="conversation not found") from None
    if str(conversation_id) != raw:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conversation_id


def build_chat_router(service: ChatService | None) -> APIRouter:
    router = APIRouter()

    def require_service() -> ChatService:
        if service is None:
            raise HTTPException(status_code=503, detail="chat provider is not configured")
        return service

    def validate_message(request: ChatRequest) -> None:
        if not request.message.strip():
            raise HTTPException(status_code=422, detail="message must contain text")

    def check_model(chat_service: ChatService, request: ChatRequest) -> None:
        """Refuse a bad choice with a fixed 400/503 before anything (a stream, a provider)."""
        try:
            chat_service.provider_for(request.model_choice)
        except UnknownModelChoice:
            raise HTTPException(status_code=400, detail="unknown model choice") from None
        except ModelUnavailable:
            raise HTTPException(status_code=503, detail="model choice unavailable") from None

    @router.post("/api/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest) -> ChatResponse:
        validate_message(request)
        chat_service = require_service()
        check_model(chat_service, request)
        try:
            result = await chat_service.complete(
                request.message, request.conversation_id, model_choice=request.model_choice
            )
        except ConversationNotFound as exc:
            raise HTTPException(status_code=404, detail="conversation not found") from exc
        except ConversationCapacityError as exc:
            raise HTTPException(status_code=503, detail="conversation capacity reached") from exc
        except ConversationStorageError as exc:
            raise HTTPException(status_code=503, detail="conversation storage unavailable") from exc
        except MemoryContextError as exc:
            raise HTTPException(status_code=503, detail="memory context unavailable") from exc
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail="chat provider failed") from exc
        return ChatResponse(
            conversation_id=result.conversation_id,
            reply=result.reply,
            provider=result.provider,
            model=result.model,
        )

    @router.get(
        "/api/chat/conversations/{conversation_id}/messages", response_model=HistoryResponse
    )
    async def conversation_messages(conversation_id: str) -> HistoryResponse:
        """Read-only: the retained messages of one known conversation (role and content only)."""
        parsed = _parse_conversation_id(conversation_id)
        chat_service = require_service()
        try:
            messages = await chat_service.store.history(parsed)
        except ConversationStorageError as exc:
            raise HTTPException(status_code=503, detail="conversation storage unavailable") from exc
        if messages is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return HistoryResponse(
            conversation_id=parsed,
            messages=[HistoryMessage(role=m.role, content=m.content) for m in messages],
        )

    @router.post("/api/chat/stream")
    async def stream_chat(
        request: ChatRequest,
        x_jarvis_activity: Annotated[str | None, Header(alias=ACTIVITY_HEADER)] = None,
    ) -> StreamingResponse:
        validate_message(request)
        chat_service = require_service()
        check_model(chat_service, request)
        with_activity = x_jarvis_activity == "1"

        async def events():
            try:
                turn = (
                    chat_service.stream_with_activity
                    if with_activity
                    else chat_service.stream
                )
                async with aclosing(
                    turn(
                        request.message,
                        request.conversation_id,
                        model_choice=request.model_choice,
                    )
                ) as items:
                    async for item in items:
                        if isinstance(item, ActivityEvent):
                            yield _sse("activity", item.to_payload())
                        elif isinstance(item, ChatDelta):
                            yield _sse("delta", {"text": item.text})
                        else:
                            yield _sse(
                                "done",
                                {
                                    "conversation_id": str(item.conversation_id),
                                    "provider": item.provider,
                                    "model": item.model,
                                },
                            )
            except ConversationNotFound:
                yield _sse("error", {"message": "conversation not found"})
            except ConversationCapacityError:
                yield _sse("error", {"message": "conversation capacity reached"})
            except ConversationStorageError:
                yield _sse("error", {"message": "conversation storage unavailable"})
            except MemoryContextError:
                yield _sse("error", {"message": "memory context unavailable"})
            except ProviderError:
                yield _sse("error", {"message": "chat provider failed"})

        return _ClosingStreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
