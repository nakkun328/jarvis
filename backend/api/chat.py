"""Chat HTTP and SSE endpoints."""

import json
from contextlib import aclosing
from uuid import UUID

from anyio import CancelScope
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send

from backend.chat.context import ConversationCapacityError, ConversationNotFound
from backend.chat.memory_context import MemoryContextError
from backend.chat.persistence import ConversationStorageError
from backend.chat.service import ChatDelta, ChatService
from backend.providers.base import ProviderError


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


class ChatResponse(BaseModel):
    conversation_id: UUID
    reply: str
    provider: str
    model: str


def _sse(event: str, data: dict[str, str]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def build_chat_router(service: ChatService | None) -> APIRouter:
    router = APIRouter()

    def require_service() -> ChatService:
        if service is None:
            raise HTTPException(status_code=503, detail="chat provider is not configured")
        return service

    def validate_message(request: ChatRequest) -> None:
        if not request.message.strip():
            raise HTTPException(status_code=422, detail="message must contain text")

    @router.post("/api/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest) -> ChatResponse:
        validate_message(request)
        chat_service = require_service()
        try:
            result = await chat_service.complete(request.message, request.conversation_id)
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

    @router.post("/api/chat/stream")
    async def stream_chat(request: ChatRequest) -> StreamingResponse:
        validate_message(request)
        chat_service = require_service()

        async def events():
            try:
                async with aclosing(
                    chat_service.stream(request.message, request.conversation_id)
                ) as items:
                    async for item in items:
                        if isinstance(item, ChatDelta):
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
