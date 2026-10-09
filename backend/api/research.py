"""Research session endpoints: read what is stored, and (only when configured) start or cancel.

The GET routes report what the research repository has persisted. Nothing is searched,
fetched, summarised or scored when a GET arrives. The two POST routes hand work to the
research run service (``backend.research.run_control.ResearchRuns``) and exist only when the
application is configured for research; otherwise they answer 503 ``research_not_configured``.
Every POST first passes the same-origin guard, even with login off.

Stored text (question, queries, titles, quotes, results) is untrusted data and is
only ever placed inside JSON string values. Responses are built from explicit
allowlists of fields, so a column added to a record later is not exposed by
accident. Error bodies carry a fixed code and never repeat anything the caller
sent or anything that was stored.
"""

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from backend.api.origin import is_cross_origin_request
from backend.research.models import (
    MAX_QUESTION_CHARS,
    RatingName,
    ResearchClaim,
    ResearchConflict,
    ResearchQueryRecord,
    ResearchSession,
    ResearchSource,
    ResearchStatus,
)
from backend.research.repository import ResearchRepository, ResearchRepositoryError
from backend.research.run_control import (
    REQUESTABLE_LEVELS,
    ResearchRuns,
    RunRefused,
)

_LOG = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100

#: The only values an error body may carry in `detail`.
ERROR_INVALID_LIMIT = "invalid_limit"
ERROR_INVALID_STATUS = "invalid_status"
ERROR_SESSION_NOT_FOUND = "session_not_found"
ERROR_STORAGE_UNAVAILABLE = "storage_unavailable"
ERROR_NOT_CONFIGURED = "research_not_configured"
ERROR_FORBIDDEN = "forbidden"
ERROR_UNSUPPORTED_MEDIA = "unsupported_media_type"
ERROR_INVALID_BODY = "invalid_body"
ERROR_QUESTION_REQUIRED = "question_required"
ERROR_QUESTION_TOO_LONG = "question_too_long"
ERROR_QUESTION_CHARACTERS = "question_invalid_characters"
ERROR_INVALID_LEVEL = "invalid_level"

MAX_BODY_BYTES = 16 * 1024


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _reuse_dto(session: ResearchSession) -> dict[str, Any] | None:
    """The past-research reuse decision: a fixed code, the earlier session and its date.

    ``prior_at`` is when the earlier research retrieved its oldest source; for a reused
    result it is the date of the shown citations, not of this run.
    """
    if session.reuse_reason is None:
        return None
    return {
        "reason": session.reuse_reason,
        "previous_session_id": str(session.reuse_of) if session.reuse_of else None,
        "prior_at": _iso(session.reuse_prior_at),
    }


def session_summary(session: ResearchSession) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "question": session.question,
        "level": session.level.value,
        "status": session.status.value,
        "failure_reason": session.failure_reason.value if session.failure_reason else None,
        "has_result": session.result_text is not None,
        "reuse": _reuse_dto(session),
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
        # How the type was decided; null for sources stored before this was recorded.
        "classification": {
            "rule": source.classification_rule,
            "basis": source.classification_basis.value if source.classification_basis else None,
        },
        "evaluation": {
            "authority": evaluation.authority,
            "freshness": evaluation.freshness,
            "primary": evaluation.primary,
            "relevance": evaluation.relevance,
            "agreement": evaluation.agreement,
        },
        # Fixed reason codes per rating (empty list: none recorded).
        "reasons": {
            name.value: [code.value for code in source.reasons.get(name)] for name in RatingName
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


def _conflict_dto(conflict: ResearchConflict) -> dict[str, Any]:
    # Ids and fixed codes only: a conflict is a flag for a reader, it carries no text.
    return {
        "id": str(conflict.id),
        "kind": conflict.kind.value,
        "status": conflict.status.value,
        "resolution": conflict.resolution.value if conflict.resolution else None,
        "claim_a_id": str(conflict.claim_a_id),
        "source_a_id": str(conflict.source_a_id),
        "claim_b_id": str(conflict.claim_b_id) if conflict.claim_b_id else None,
        "source_b_id": str(conflict.source_b_id),
        "detected_at": _iso(conflict.detected_at),
        "resolved_at": _iso(conflict.resolved_at),
    }


def session_detail(
    session: ResearchSession,
    queries: list[ResearchQueryRecord],
    sources: list[ResearchSource],
    claims: list[ResearchClaim],
    conflicts: list[ResearchConflict] | None = None,
    progress: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    detail = session_summary(session)
    detail["result_text"] = session.result_text
    detail["queries"] = [_query_dto(query) for query in queries]
    detail["sources"] = [_source_dto(source) for source in sources]
    detail["claims"] = [_claim_dto(claim) for claim in claims]
    detail["conflicts"] = [_conflict_dto(conflict) for conflict in conflicts or []]
    if progress is not None:
        # Present only while this process is running the session: a stage code and counters.
        detail["progress"] = dict(progress)
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


def _validated_question(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=ERROR_QUESTION_REQUIRED)
    if len(value) > MAX_QUESTION_CHARS:
        raise HTTPException(status_code=422, detail=ERROR_QUESTION_TOO_LONG)
    if any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in value):
        raise HTTPException(status_code=422, detail=ERROR_QUESTION_CHARACTERS)
    return value


async def _read_body(request: Request) -> dict[str, Any]:
    """The JSON object of a POST body, size-bounded. Nothing of it is echoed on failure."""
    media = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != "application/json":
        raise HTTPException(status_code=415, detail=ERROR_UNSUPPORTED_MEDIA)
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=ERROR_INVALID_BODY)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail=ERROR_INVALID_BODY)
    try:
        data = json.loads(bytes(body))
    except (ValueError, RecursionError):
        raise HTTPException(status_code=422, detail=ERROR_INVALID_BODY) from None
    if not isinstance(data, dict) or set(data) != {"question", "level"}:
        raise HTTPException(status_code=422, detail=ERROR_INVALID_BODY)
    return data


def create_research_router(
    repository: ResearchRepository,
    runs: ResearchRuns | None = None,
    *,
    unavailable_reason: str | None = None,
    trusted_proxy: bool = False,
) -> APIRouter:
    """Build the research router.

    ``runs`` is the run service when research is configured. Without it the status route
    reports ``unavailable_reason`` and the POST routes answer 503. The endpoints are sync
    functions (or hand blocking work to the thread pool), so a slow disk never blocks the
    event loop. Each repository call opens and closes its own short-lived SQLite connection.
    """
    router = APIRouter()

    def storage_unavailable(exc: ResearchRepositoryError) -> HTTPException:
        _LOG.warning("Research storage failed: %s", type(exc).__name__)
        return HTTPException(status_code=503, detail=ERROR_STORAGE_UNAVAILABLE)

    def guard_post(request: Request) -> ResearchRuns:
        if is_cross_origin_request(request.scope, trusted_proxy):
            raise HTTPException(status_code=403, detail=ERROR_FORBIDDEN)
        if runs is None:
            raise HTTPException(status_code=503, detail=ERROR_NOT_CONFIGURED)
        return runs

    @router.get("/api/research/status")
    def research_status() -> dict[str, Any]:
        if runs is not None:
            return {"enabled": True}
        return {"enabled": False, "reason": unavailable_reason or "disabled"}

    @router.post("/api/research/sessions", status_code=202)
    async def create_session(request: Request) -> dict[str, str]:
        service = guard_post(request)
        data = await _read_body(request)
        question = _validated_question(data["question"])
        level_value = data["level"]
        level = next(
            (item for item in REQUESTABLE_LEVELS if item.value == level_value), None
        ) if isinstance(level_value, str) else None
        if level is None:
            raise HTTPException(status_code=422, detail=ERROR_INVALID_LEVEL)
        try:
            session_id = await run_in_threadpool(service.submit, question, level)
        except RunRefused as refusal:
            raise HTTPException(status_code=refusal.status_code, detail=refusal.code) from None
        except ResearchRepositoryError as exc:
            raise storage_unavailable(exc) from exc
        return {"id": str(session_id)}

    @router.post("/api/research/sessions/{session_id}/cancel")
    def cancel_session(session_id: str, request: Request) -> dict[str, str]:
        service = guard_post(request)
        parsed_id = _parse_session_id(session_id)
        try:
            outcome = service.cancel(parsed_id)
        except RunRefused as refusal:
            raise HTTPException(status_code=refusal.status_code, detail=refusal.code) from None
        except ResearchRepositoryError as exc:
            raise storage_unavailable(exc) from exc
        return {"id": str(parsed_id), "status": outcome}

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
            conflicts = repository.list_conflicts(parsed_id)
        except ResearchRepositoryError as exc:
            raise storage_unavailable(exc) from exc
        progress = runs.progress(parsed_id) if runs is not None else None
        return session_detail(session, queries, sources, claims, conflicts, progress)

    return router
