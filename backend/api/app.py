"""FastAPI application factory and health endpoints."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api.chat import build_chat_router
from backend.api.request_logging import RequestLoggingMiddleware
from backend.api.research import create_research_router
from backend.api.tasks import create_tasks_router
from backend.chat.memory_context import MemoryContext
from backend.chat.persistence import SQLiteConversationStore
from backend.chat.semantic_context import SemanticMemoryContext
from backend.chat.service import ChatService
from backend.core.config import ConfigError, Settings
from backend.core.database import Database
from backend.core.logging import configure_logging
from backend.memory.embedding import EmbeddingProvider
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository
from backend.memory.retrieval import MemoryRetriever
from backend.memory.semantic import SemanticMemorySearcher
from backend.memory.vector import VectorIndex
from backend.personality.settings import PersonalityError, load_personality
from backend.providers.base import LLMProvider
from backend.providers.factory import create_provider
from backend.research.repository import ResearchRepository
from backend.tasks.repository import TaskRepository


def create_app(
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
    *,
    embedding_provider: EmbeddingProvider | None = None,
    memory_index: VectorIndex | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    try:
        personality = load_personality(settings.personality_path)
    except PersonalityError as error:
        raise ConfigError(f"Invalid personality settings: {error}") from None
    if (embedding_provider is None) != (memory_index is None):
        raise ConfigError("Semantic chat requires both an embedding provider and memory index")
    if embedding_provider is not None and settings.memory_vault_path is None:
        raise ConfigError("Semantic chat requires an explicitly configured memory vault")
    database = Database(settings.db_path)
    memory_context = None
    if settings.memory_vault_path is not None:
        vault_path = settings.memory_vault_path
        if vault_path.is_symlink() or not vault_path.is_dir():
            raise ConfigError("JARVIS_MEMORY_VAULT_PATH must be an existing vault directory")
        retriever = MemoryRetriever(
            MemoryRepository(database), ObsidianVault(vault_path), vector_index=memory_index
        )
        memory_context = (
            SemanticMemoryContext(SemanticMemorySearcher(retriever, embedding_provider))
            if embedding_provider is not None
            else MemoryContext(retriever)
        )
    if provider is None:
        provider = create_provider(settings)
    chat_service = (
        ChatService(
            provider,
            SQLiteConversationStore(database),
            memory_context=memory_context,
            personality=personality,
        )
        if provider is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        configure_logging(settings.log_level)
        try:
            database.initialize()
            logging.getLogger(__name__).info("JARVIS backend started")
            logging.getLogger(__name__).info(
                "Personality (%s): %s",
                "file" if settings.personality_path is not None else "default",
                personality.describe(),
            )
            yield
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()

    app = FastAPI(
        title="JARVIS",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/health/live")
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness() -> dict[str, str]:
        if not database.is_ready():
            raise HTTPException(status_code=503, detail="database unavailable")
        return {"status": "ok"}

    app.include_router(build_chat_router(chat_service))
    app.include_router(create_research_router(ResearchRepository(database)))
    app.include_router(create_tasks_router(TaskRepository(database)))

    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
    if (frontend_dir / "index.html").is_file():
        app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

        @app.get("/", include_in_schema=False)
        def web_client() -> FileResponse:
            return FileResponse(frontend_dir / "index.html")

        @app.get("/research", include_in_schema=False)
        def research_client() -> FileResponse:
            return FileResponse(frontend_dir / "research.html")

    return app


app = create_app()
