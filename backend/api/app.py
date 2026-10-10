"""FastAPI application factory and health endpoints."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api.approvals import create_approvals_router
from backend.api.chat import build_chat_router
from backend.api.devices import create_devices_router
from backend.api.memory import create_memory_router
from backend.api.memory_withdraw import create_memory_withdraw_router
from backend.api.models import create_models_router
from backend.api.request_logging import RequestLoggingMiddleware
from backend.api.research import create_research_router
from backend.api.research_memory import create_research_memory_router
from backend.api.tasks import create_tasks_router
from backend.auth.middleware import AuthMiddleware
from backend.auth.routes import create_auth_router
from backend.auth.service import AuthService
from backend.chat.casual import CasualService
from backend.chat.memory_context import MemoryContext
from backend.chat.persistence import SQLiteConversationStore
from backend.chat.research_start import RunServiceStarter
from backend.chat.semantic_context import SemanticMemoryContext
from backend.chat.service import ChatService
from backend.core.config import ConfigError, Settings
from backend.core.database import Database
from backend.core.logging import configure_logging
from backend.memory.auto_approval import AUTO_APPROVER
from backend.memory.embedding import EmbeddingProvider
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository
from backend.memory.retrieval import MemoryRetriever
from backend.memory.semantic import SemanticMemorySearcher
from backend.memory.vector import VectorIndex
from backend.memory.writer import MemoryWriter
from backend.personality.prompt import render_system_prompt
from backend.personality.settings import PersonalityError, load_personality
from backend.providers.base import LLMProvider
from backend.providers.choices import ModelRegistry
from backend.providers.factory import create_provider
from backend.research.memory_candidates import (
    RefusedCandidates,
    ResearchMemoryCandidates,
)
from backend.research.models import ResearchLevel
from backend.research.repository import ResearchRepository
from backend.research.run_control import (
    REASON_DISABLED,
    REASON_NO_CHAT_PROVIDER,
    REASON_NO_SEARCH_PROVIDER,
)
from backend.research.search import SearchProvider
from backend.research.search_factory import create_search_provider
from backend.router import AuditedRouter, InMemoryAuditSink, LLMRouter, Router, RuleRouter
from backend.tasks.repository import TaskRepository
from backend.tools.approvals import ApprovalStore

if TYPE_CHECKING:
    from backend.research.standard import PageFetcher


def _build_router(settings: Settings, provider: LLMProvider | None) -> Router:
    """The router chosen by ``JARVIS_ROUTER``, with an in-memory audit (no text, not persisted).

    ``llm`` reuses the already configured chat provider: no new key, model or endpoint. An
    injected ``router=`` is used as given and is not wrapped.
    """
    if settings.router == "rule":
        inner: Router = RuleRouter()
    elif settings.router == "llm":
        if provider is None:
            raise ConfigError("JARVIS_ROUTER=llm requires a configured chat provider")
        inner = LLMRouter(provider)
    else:  # pragma: no cover - Settings validates the value
        raise ConfigError("JARVIS_ROUTER is invalid")
    return AuditedRouter(inner, InMemoryAuditSink())


def create_app(
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
    *,
    embedding_provider: EmbeddingProvider | None = None,
    memory_index: VectorIndex | None = None,
    auth: AuthService | None = None,
    search_provider: SearchProvider | None = None,
    page_reader: "PageFetcher | None" = None,
    router: Router | None = None,
    models: ModelRegistry | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    auth = auth or AuthService.from_settings(settings)
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
    research_repository = ResearchRepository(database)
    # Owner-approved exception (docs/memory.md): approve research candidates automatically. It
    # needs the vault (Settings enforces this) and goes through the normal MemoryWriter.
    memory_writer = (
        MemoryWriter(MemoryRepository(database), ObsidianVault(settings.memory_vault_path))
        if settings.memory_vault_path is not None
        else None
    )
    research_memory = ResearchMemoryCandidates(
        research_repository,
        MemoryRepository(database),
        approve=(
            (lambda memory_id: memory_writer.approve(memory_id, actor=AUTO_APPROVER))
            if settings.research_memory_auto_approve and memory_writer is not None
            else None
        ),
    )

    def stage_on_completion(session_id: UUID) -> None:
        try:
            research_memory.stage(session_id)
        except RefusedCandidates:
            pass  # nothing eligible; not an error

    run_service, research_reason = _build_research(
        settings,
        database,
        research_repository,
        provider,
        search_provider,
        page_reader,
        on_completed=stage_on_completion if settings.research_memory_auto_stage else None,
    )
    if router is None and settings.router != "off":
        router = _build_router(settings, provider)
    # The selectable models need a default provider to fall back on; without one (or without
    # JARVIS_MODEL_CHOICES) there is no registry and the app is exactly the single-model app.
    if models is None and provider is not None and settings.model_choices:
        models = ModelRegistry(settings.model_choices, provider)
    chat_service = (
        ChatService(
            provider,
            SQLiteConversationStore(database),
            memory_context=memory_context,
            personality=personality,
            models=models,
            router=router,
            # The casual path needs the switch, a router and a chat provider; any one missing
            # leaves every turn on the Main Agent path as before.
            casual=(
                CasualService(
                    system_prompt=render_system_prompt(personality),
                    daily_call_limit=settings.casual_daily_call_limit,
                )
                if settings.casual and router is not None
                else None
            ),
            # A routed research decision starts a research only when there is a router AND the
            # research run service exists (switch, search provider and chat provider all set).
            research_starter=(
                RunServiceStarter(run_service, ResearchLevel(settings.chat_research_level))
                if router is not None and run_service is not None
                else None
            ),
        )
        if provider is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        configure_logging(settings.log_level)
        worker: asyncio.Task[None] | None = None
        try:
            database.initialize()
            logging.getLogger(__name__).info("JARVIS backend started")
            auth.log_startup()
            if run_service is not None:
                # Settle what a previous process left behind (nothing is re-run), then work.
                run_service.recover()
                worker = asyncio.create_task(run_service.run_worker(), name="research-worker")
                logging.getLogger(__name__).info("Web research is enabled")
            logging.getLogger(__name__).info(
                "Personality (%s): %s",
                "file" if settings.personality_path is not None else "default",
                personality.describe(),
            )
            yield
        finally:
            if worker is not None:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            if models is not None:
                await models.aclose()
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
    if auth.enabled:
        # Added before the request logger so the logger is outermost and records refusals too.
        app.add_middleware(AuthMiddleware, auth=auth)
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
    app.include_router(create_models_router(models))
    app.include_router(create_memory_router(MemoryRepository(database)))
    app.include_router(
        create_research_router(
            research_repository,
            run_service,
            unavailable_reason=research_reason,
            trusted_proxy=settings.trusted_proxy,
        )
    )
    app.include_router(
        create_research_memory_router(research_memory, trusted_proxy=settings.trusted_proxy)
    )
    app.include_router(
        create_memory_withdraw_router(
            MemoryRepository(database), memory_writer, trusted_proxy=settings.trusted_proxy
        )
    )
    app.include_router(create_tasks_router(TaskRepository(database)))
    app.include_router(
        create_approvals_router(ApprovalStore(database), trusted_proxy=settings.trusted_proxy)
    )
    app.include_router(
        create_devices_router(
            settings, login_enabled=auth.enabled, trusted_proxy=settings.trusted_proxy
        )
    )

    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
    if auth.enabled:
        app.include_router(create_auth_router(auth, frontend_dir))
    if (frontend_dir / "index.html").is_file():
        app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

        @app.get("/", include_in_schema=False)
        def web_client() -> FileResponse:
            return FileResponse(frontend_dir / "index.html")

        @app.get("/tasks", include_in_schema=False)
        def tasks_client() -> FileResponse:
            return FileResponse(frontend_dir / "tasks.html")

        @app.get("/approvals", include_in_schema=False)
        def approvals_client() -> FileResponse:
            return FileResponse(frontend_dir / "approvals.html")

        @app.get("/devices", include_in_schema=False)
        def devices_client() -> FileResponse:
            return FileResponse(frontend_dir / "devices.html")

        @app.get("/research", include_in_schema=False)
        def research_client() -> FileResponse:
            return FileResponse(frontend_dir / "research.html")

        @app.get("/memory", include_in_schema=False)
        def memory_client() -> FileResponse:
            return FileResponse(frontend_dir / "memory.html")

        @app.get("/manifest.webmanifest", include_in_schema=False)
        def web_manifest() -> FileResponse:
            return FileResponse(
                frontend_dir / "manifest.webmanifest",
                media_type="application/manifest+json",
                headers={"Cache-Control": "no-cache"},
            )

        @app.get("/sw.js", include_in_schema=False)
        def service_worker() -> FileResponse:
            # Served from the root so its scope can be "/". It must never be cached
            # by the browser's HTTP cache, or a fixed worker could not reach users.
            return FileResponse(
                frontend_dir / "sw.js",
                media_type="text/javascript",
                headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
            )

    return app


def _build_research(
    settings: Settings,
    database: Database,
    repository: ResearchRepository,
    chat_provider: LLMProvider | None,
    search_provider: SearchProvider | None,
    page_reader: "PageFetcher | None",
    on_completed: Callable[[UUID], None] | None = None,
):
    """The research run service and, when there is none, the fixed reason why not.

    Research needs all of: the switch, a search provider, and a chat provider. The pipeline
    (and the HTTP client it needs) is imported only when all three are present.
    """
    if not settings.research_enabled:
        return None, REASON_DISABLED
    search = search_provider or create_search_provider(settings, repository=repository)
    if search is None:
        return None, REASON_NO_SEARCH_PROVIDER
    if chat_provider is None:
        return None, REASON_NO_CHAT_PROVIDER
    try:
        from backend.research.reader import PageReader
        from backend.research.runner import TASK_TIMEOUT_SECONDS, ResearchRunService
    except ImportError as exc:
        raise ConfigError("Install httpx to use web research") from exc
    from backend.tasks.queue import TaskQueue

    exhausted = getattr(search, "is_exhausted", None)
    service = ResearchRunService(
        repository,
        TaskQueue(TaskRepository(database), timeout_seconds=TASK_TIMEOUT_SECONDS),
        search=search,
        reader=page_reader or PageReader(),
        llm=chat_provider,
        is_budget_exhausted=exhausted if callable(exhausted) else None,
        on_completed=on_completed,
    )
    return service, None


app = create_app()
