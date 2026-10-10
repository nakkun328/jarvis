"""Read-only feed of "a memory was just made" events (docs/memory.md, "Memory activity feed").

``GET /api/memory/activity?after=<seq>`` returns ``{latest, events, configured, chat_enabled,
research_enabled}``: the events with ``seq > after`` (at most 50, oldest first), the newest ``seq``
the process has issued, and which memory sources are switched on. ``after`` must be a
non-negative integer (default 0), else 422 ``invalid_after``. It sits behind the login layer and
is never cached. Events hold the owner's own (shortened) note text, so they are not logged.
"""

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from backend.memory.events import MAX_PAGE, MemoryActivityFeed

ERROR_INVALID_AFTER = "invalid_after"
_NO_STORE = {"Cache-Control": "no-store"}
_AFTER = re.compile(r"[0-9]{1,15}")


def create_memory_activity_router(
    feed: MemoryActivityFeed,
    *,
    configured: bool,
    chat_enabled: bool,
    research_enabled: bool,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/memory/activity")
    def activity(after: str | None = None) -> JSONResponse:
        if after is None:
            cursor = 0
        elif _AFTER.fullmatch(after):
            cursor = int(after)
        else:
            raise HTTPException(status_code=422, detail=ERROR_INVALID_AFTER)
        return JSONResponse(
            {
                "latest": feed.latest,
                "events": feed.since(cursor, MAX_PAGE),
                "configured": configured,
                "chat_enabled": chat_enabled,
                "research_enabled": research_enabled,
            },
            headers=_NO_STORE,
        )

    return router
