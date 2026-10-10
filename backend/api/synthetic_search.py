"""Explicit artificial-memory browser app, separate from the normal application."""

import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from anyio import CancelScope
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.memory.chroma import ChromaVectorIndex
from backend.memory.evaluation import parse_dataset, prepare_synthetic_corpus
from backend.memory.semantic import SemanticMemorySearcher

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/semantic-evaluation-ja-extra-v1.json"


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4000, strict=True)


class SyntheticSearchSession:
    """Own one lazily prepared corpus; serialize inference and shutdown."""

    def __init__(self, provider_factory, *, limit=3, contract_only=False):
        self.provider_factory = provider_factory
        self.limit = limit
        self.contract_only = contract_only
        self.lock = asyncio.Lock()
        self.provider = None
        self.directory = None
        self.index = None
        self.searcher = None
        self.ids = {}
        self.corrections = {}
        self.closed = False

    async def _release(self):
        # The encoder close waits for any cancelled CPU inference before index
        # teardown and temporary-file deletion. Attempt every cleanup on failure.
        failed = False
        with CancelScope(shield=True):
            try:
                close = getattr(self.provider, "aclose", None)
                if close is not None:
                    await close()
            except Exception:
                failed = True
            try:
                close = getattr(getattr(self.index, "client", None), "close", None)
                if close is not None:
                    await asyncio.to_thread(close)
            except Exception:
                failed = True
            try:
                if self.directory is not None:
                    self.directory.cleanup()
            except Exception:
                failed = True
            self.searcher = self.provider = self.index = self.directory = None
            self.ids = {}
            self.corrections = {}
        if failed:
            raise RuntimeError("synthetic search cleanup failed")

    async def _prepare(self):
        try:
            self.provider = self.provider_factory()
            self.directory = tempfile.TemporaryDirectory(prefix="jarvis-synthetic-browser-")
            dataset = parse_dataset(json.loads(FIXTURE.read_text(encoding="utf-8")))
            self.corrections = {
                n["replacement_id"]: n["id"] for n in dataset.notes if n["status"] == "superseded"
            }

            def index_factory(path):
                self.index = ChromaVectorIndex(path)
                return self.index

            retriever, self.ids, _ = await prepare_synthetic_corpus(
                Path(self.directory.name), self.provider, dataset, index_factory=index_factory
            )
            self.searcher = SemanticMemorySearcher(retriever, self.provider)
        except BaseException:
            await self._release()
            raise

    async def search(self, query):
        async with self.lock:
            if self.closed:
                raise RuntimeError("synthetic search is closed")
            if self.searcher is None:
                await self._prepare()
            result = await self.searcher.search(query, limit=self.limit)
            if result.issues:
                raise RuntimeError("current approved notes could not be verified")
            matches = []
            for match in result.matches:
                record = match.record
                name = self.ids[str(record.id)]
                matches.append({
                    "id": str(record.id), "fixture_id": name,
                    "body": record.content, "source": record.source,
                    "origin": record.origin.value, "revision": match.note_revision,
                    "memory_confidence": record.confidence, "importance": record.importance,
                    "index_score": match.match_score,
                    "edited_since_approval": match.edited_since_approval,
                    "stale": match.stale, "corrects_fixture_id": self.corrections.get(name),
                })
            return {"matches": matches, "contract_only": self.contract_only,
                    "support_assessment": "not_assessed"}

    async def aclose(self):
        async with self.lock:
            self.closed = True
            await self._release()


def create_synthetic_app(provider_factory, *, limit=3, contract_only=False):
    """Entrypoint injects only fixed E5 or explicitly selected contract vectors.

    No Settings, normal app, LLM provider, caller storage or fixture paths are
    accepted. The factory is server-side dependency injection, never an HTTP arg.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    session = SyntheticSearchSession(provider_factory, limit=limit, contract_only=contract_only)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await session.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # Loopback-only listener: also refuse foreign Host headers (DNS rebinding).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.get("/api/synthetic-search/status")
    async def status():
        return {"contract_only": contract_only, "prepared": session.searcher is not None,
                "synthetic": True, "fixture": "ja-extra-v1"}

    @app.post("/api/synthetic-search")
    async def search(request: SearchRequest):
        if not request.query.strip():
            raise HTTPException(status_code=422, detail="question must contain text")
        try:
            return await session.search(request.query)
        except Exception as exc:
            # Raw dependency errors, request bodies and paths never reach the UI.
            raise HTTPException(status_code=503, detail="synthetic search unavailable") from exc

    frontend = REPO / "frontend"
    app.mount("/static", StaticFiles(directory=frontend), name="static")

    @app.get("/", include_in_schema=False)
    async def page():
        return FileResponse(frontend / "synthetic-search.html")

    return app
