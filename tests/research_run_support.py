"""Fakes for the research run tests: search, pages, model and a ready-made service.

Nothing here opens a socket. Pages are served by a fake transport behind the real safe reader,
the search provider is canned, and the model is scripted.
"""

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from backend.core.database import Database
from backend.providers.base import CompletionRequest, CompletionResponse
from backend.research.mock_search import MockSearchProvider
from backend.research.reader import PageReader, TransportRequest, TransportResponse
from backend.research.repository import ResearchRepository
from backend.research.runner import ResearchRunService
from backend.research.search import SearchError, SearchFailure, SearchQuery
from backend.tasks.queue import TaskQueue
from backend.tasks.repository import TaskRepository

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
PUBLIC = "93.184.216.34"

QUESTION = "How long does the Foo widget cache keep entries?"
MARKER_TEXT = "zebra-lantern-1729"  # a marker that must never reach a log or an error body

URL_A = "https://docs.a.test/foo-cache"
URL_B = "https://docs.b.test/foo-cache-notes"
URL_C = "https://news.c.test/foo-cache-news"

SIXTY_A = "The Foo widget cache keeps entries for 60 seconds."
SIXTY_B = "Foo widget cache entries are kept for 60 seconds."
SIXTY_C = "A Foo widget cache keeps each entry for 60 seconds."


def page(title: str, *paragraphs: str) -> str:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>"


def foo_page(title: str, fact: str) -> str:
    return page(title, "How long does the Foo widget cache keep entries?", fact)


def hit(url: str, title: str) -> dict[str, object]:
    return {"url": url, "title": title, "snippet": f"snippet for {title}"}


AGREEING_PAGES = {
    URL_A: foo_page("Foo Cache Docs", SIXTY_A),
    URL_B: foo_page("Foo Cache Notes", SIXTY_B),
    URL_C: foo_page("Foo Cache News", SIXTY_C),
}
AGREEING_HITS = [
    hit(URL_A, "Foo Cache Docs"),
    hit(URL_B, "Foo Cache Notes"),
    hit(URL_C, "Foo Cache News"),
]
AGREEING_CLAIMS = {
    URL_A: [(SIXTY_A, SIXTY_A)],
    URL_B: [(SIXTY_B, SIXTY_B)],
    URL_C: [(SIXTY_C, SIXTY_C)],
}


class FakeTransport:
    def __init__(self, routes: Mapping[str, object]) -> None:
        self.routes = dict(routes)
        self.requests: list[str] = []

    async def fetch(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request.url)
        route = self.routes[request.url]
        if isinstance(route, BaseException):
            raise route
        assert isinstance(route, str)
        headers = {"content-type": "text/html; charset=utf-8"}
        return TransportResponse(200, headers, route.encode(), False)


class FakeResolver:
    async def __call__(self, host: str) -> Sequence[str]:
        return [PUBLIC]


class FakeSearch:
    """Canned hits for every query; can fail, or wait for a gate (to test cancellation)."""

    name = "fake"

    def __init__(
        self,
        hits: Sequence[Mapping[str, object]] = (),
        *,
        failure: SearchFailure | None = None,
        gate: "Gate | None" = None,
    ) -> None:
        self.hits = list(hits)
        self.failure = failure
        self.gate = gate
        self.calls: list[str] = []

    async def search(self, query: SearchQuery):
        self.calls.append(query.text)
        if self.gate is not None:
            await self.gate.wait()
        if self.failure is not None:
            raise SearchError(self.failure)
        return await MockSearchProvider({query.text: self.hits}).search(query)


class Gate:
    """A thread-safe latch an async fake waits on until a test releases it."""

    def __init__(self) -> None:
        import threading

        self._open = threading.Event()
        self._entered = threading.Event()

    def release(self) -> None:
        self._open.set()

    @property
    def entered(self) -> bool:
        return self._entered.is_set()

    def wait_until_entered(self, timeout: float = 5.0) -> bool:
        return self._entered.wait(timeout)

    async def wait(self) -> None:
        self._entered.set()
        while not self._open.is_set():
            await asyncio.sleep(0.01)


class ScriptedLLM:
    """Proposes the claims it is told about for the sources it is shown."""

    def __init__(
        self,
        claims: Mapping[str, Sequence[tuple[str, str]]] | None = None,
        *,
        insufficient: bool = False,
        raw: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.claims = dict(claims or {})
        self.insufficient = insufficient
        self.raw = raw
        self.error = error
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return CompletionResponse(text=self.raw, provider="fake", model="scripted")
        user = request.messages[1].content
        proposed = []
        for number, url in re.findall(r'<evidence number="(\d+)">\ntitle: .*\nurl: (\S+)', user):
            for text, quote in self.claims.get(url, ()):
                proposed.append({"text": text, "source": int(number), "quote": quote})
        reply = {
            "answer": "FREE TEXT THAT MUST NEVER APPEAR IN THE RESULT",
            "insufficient_evidence": self.insufficient,
            "claims": proposed,
        }
        return CompletionResponse(text=json.dumps(reply), provider="fake", model="scripted")

    def stream(self, request: CompletionRequest):  # pragma: no cover - not used
        raise NotImplementedError


def build_reader(pages: Mapping[str, object]) -> tuple[PageReader, FakeTransport]:
    transport = FakeTransport(pages)
    return PageReader(transport, FakeResolver(), clock=lambda: NOW), transport


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        search: FakeSearch | None = None,
        pages: Mapping[str, object] | None = None,
        llm: ScriptedLLM | None = None,
        is_budget_exhausted=None,
        queue_timeout: float = 30.0,
        quick_limits=None,
        standard_limits=None,
        clock=None,
    ) -> None:
        self.database = Database(tmp_path / "run.sqlite3")
        self.database.initialize()
        self.repository = ResearchRepository(self.database)
        self.tasks = TaskRepository(self.database)
        self.queue = TaskQueue(self.tasks, timeout_seconds=queue_timeout, cancel_grace_seconds=1.0)
        self.search = search or FakeSearch(AGREEING_HITS)
        self.reader, self.transport = build_reader(pages if pages is not None else AGREEING_PAGES)
        self.llm = llm or ScriptedLLM(AGREEING_CLAIMS)
        self.service = ResearchRunService(
            self.repository,
            self.queue,
            search=self.search,
            reader=self.reader,
            llm=self.llm,
            is_budget_exhausted=is_budget_exhausted,
            poll_seconds=0.02,
            quick_limits=quick_limits,
            standard_limits=standard_limits,
            clock=clock,
        )

    def task_of(self, session_id):
        goal = f"web research {session_id}"
        return next(t for t in self.tasks.list_tasks(limit=100) if t.goal == goal)
