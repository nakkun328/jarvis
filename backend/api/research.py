"""Read-only research session endpoints.

Everything here reports what the research repository has persisted. Nothing is
searched, fetched, summarised or scored when a request arrives, and there are no
write routes: the stored data is inspected, never changed, from this API.

Stored text (question, queries, titles, quotes, results) is untrusted data and is
only ever placed inside JSON string values. Responses are built from explicit
allowlists of fields, so a column added to a record later is not exposed by
accident. Error bodies carry a fixed code and never repeat anything the caller
sent or anything that was stored.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException

from backend.research.models import (
    ResearchClaim,
    ResearchQueryRecord,
    ResearchSession,
    ResearchSource,
    ResearchStatus,
)
from backend.research.repository import ResearchRepository, ResearchRepositoryError

_LOG = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100

#: The only values an error body may carry in `detail`.
ERROR_INVALID_LIMIT = "invalid_limit"
ERROR_INVALID_STATUS = "invalid_status"
ERROR_SESSION_NOT_FOUND = "session_not_found"
ERROR_STORAGE_UNAVAILABLE = "storage_unavailable"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def session_summary(session: ResearchSession) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "question": session.question,
        "level": session.level.value,
        "status": session.status.value,
        "failure_reason": session.failure_reason.value if session.failure_reason else None,
        "has_result": session.result_text is not None,
        "created_at": _iso(session.created_at),
        "updated_at": _iso(session.updated_at),
    }


def _query_dto(query: ResearchQueryRecord) -> dict[str, Any]:
    return {"position": query.position, "text": query.text, "created_at": _iso(query.created_at)}


def _source_dto(source: ResearchSource) -> dict[str, Any]:
    # The content digest is internal bookkeeping and is deliberately not exposed.
    evaluation = source.evaluation
    return {
        "id": str(source.id),
        "url": source.url,
        "final_url": source.final_url,
        "title": source.title,
        "publisher": source.publisher,
        "published_at": _iso(source.published_at),
        "retrieved_at": _iso(source.retrieved_at),
        "source_type": source.source_type.value,
        "evaluation": {
            "authority": evaluation.authority,
            "freshness": evaluation.freshness,
            "primary": evaluation.primary,
            "relevance": evaluation.relevance,
            "agreement": evaluation.agreement,
        },
    }


def _claim_dto(claim: ResearchClaim) -> dict[str, Any]:
    return {
        "id": str(claim.id),
        "claim_text": claim.claim_text,
        "source_id": str(claim.source_id),
        "quote": claim.quote,
        "quote_start": claim.quote_start,
        "quote_end": claim.quote_end,
    }


def session_detail(
    session: ResearchSession,
    queries: list[ResearchQueryRecord],
    sources: list[ResearchSource],
    claims: list[ResearchClaim],
) -> dict[str, Any]:
    detail = session_summary(session)
    detail["result_text"] = session.result_text
    detail["queries"] = [_query_dto(query) for query in queries]
    detail["sources"] = [_source_dto(source) for source in sources]
    detail["claims"] = [_claim_dto(claim) for claim in claims]
    return detail


def _parse_limit(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_LIST_LIMIT
    # isascii() keeps non-ASCII digits (which int() would accept) out of the parser.
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= MAX_LIST_LIMIT:
        raise HTTPException(status_code=422, detail=ERROR_INVALID_LIMIT)
    return int(raw)


def _parse_status(raw: str | None) -> ResearchStatus | None:
    if raw is None:
        return None
    try:
        return ResearchStatus(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail=ERROR_INVALID_STATUS) from None


def _parse_session_id(raw: str) -> UUID:
    # Only the canonical lowercase hyphenated form is accepted, so every session has
    # exactly one URL. Anything else is the same 404 as an id that does not exist.
    try:
        session_id = UUID(raw)
    except ValueError:
        raise HTTPException(status_code=404, detail=ERROR_SESSION_NOT_FOUND) from None
    if str(session_id) != raw:
        raise HTTPException(status_code=404, detail=ERROR_SESSION_NOT_FOUND)
    return session_id


def create_research_router(repository: ResearchRepository) -> APIRouter:
    """Build the read-only research router.

    The endpoints are sync functions, so FastAPI runs them in its thread pool and
    a slow disk never blocks the event loop. Each repository call opens and closes
    its own short-lived read-only SQLite connection.
    """
    router = APIRouter()

    def storage_unavailable(exc: ResearchRepositoryError) -> HTTPException:
        _LOG.warning("Research storage failed: %s", type(exc).__name__)
        return HTTPException(status_code=503, detail=ERROR_STORAGE_UNAVAILABLE)

    @router.get("/api/research/sessions")
    def list_sessions(
        status: str | None = None, limit: str | None = None
    ) -> dict[str, Any]:
        parsed_status = _parse_status(status)
        parsed_limit = _parse_limit(limit)
        try:
            sessions = repository.list_sessions(
                parsed_status, limit=parsed_limit, newest_first=True
            )
        except ResearchRepositoryError as exc:
            raise storage_unavailable(exc) from exc
        return {"sessions": [session_summary(session) for session in sessions]}

    @router.get("/api/research/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        parsed_id = _parse_session_id(session_id)
        try:
            session = repository.get_session(parsed_id)
            if session is None:
                raise HTTPException(status_code=404, detail=ERROR_SESSION_NOT_FOUND)
            queries = repository.list_queries(parsed_id)
            sources = repository.list_sources(parsed_id)
            claims = repository.list_claims(parsed_id)
        except ResearchRepositoryError as exc:
            raise storage_unavailable(exc) from exc
        return session_detail(session, queries, sources, claims)

    return router
