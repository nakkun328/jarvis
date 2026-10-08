"""Request correlation IDs and one access-style log record per HTTP request."""

import logging
import re
from time import perf_counter
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.core.logging import REQUEST_ID_KEY, log_context

REQUEST_ID_HEADER = "X-Request-ID"
_HEADER_NAME = REQUEST_ID_HEADER.lower().encode("latin-1")
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,63}")
_MAX_PATH_CHARS = 200

_LOG = logging.getLogger("jarvis.access")


def inbound_request_id(scope: Scope) -> str | None:
    """Return the caller's request ID when exactly one well-formed header is present."""
    values = [value for name, value in scope.get("headers", ()) if name == _HEADER_NAME]
    if len(values) != 1:
        return None
    candidate = values[0].decode("latin-1")
    return candidate if _VALID_REQUEST_ID.fullmatch(candidate) else None


def _route_path(scope: Scope) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str):
        return template
    return str(scope.get("path", ""))[:_MAX_PATH_CHARS]


class RequestLoggingMiddleware:
    """Pure ASGI middleware, so contextvars and streaming (SSE) responses are unaffected.

    It records only method, route template (or the raw path when no route matched, never the
    query string), status, duration, and the exception type. Bodies and headers are never read.
    An unhandled exception is re-raised after logging; the 500 response that Starlette then
    builds outside this middleware does not carry the request ID header.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = inbound_request_id(scope) or str(uuid4())
        status: int | None = None

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = [
                    (name, value)
                    for name, value in message.get("headers", ())
                    if name.lower() != _HEADER_NAME
                ]
                headers.append((_HEADER_NAME, request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        with log_context(**{REQUEST_ID_KEY: request_id}):
            started = perf_counter()
            failure: BaseException | None = None
            try:
                await self.app(scope, receive, send_with_id)
            except BaseException as exc:
                failure = exc
                raise
            finally:
                self._log(scope, status, started, failure)

    @staticmethod
    def _log(
        scope: Scope, status: int | None, started: float, failure: BaseException | None
    ) -> None:
        extra: dict[str, object] = {
            "method": str(scope.get("method", "")),
            "path": _route_path(scope),
            "status": status if status is not None or failure is None else 500,
            "duration_ms": round((perf_counter() - started) * 1000, 2),
        }
        if failure is None:
            _LOG.log(
                logging.WARNING if status is not None and status >= 500 else logging.INFO,
                "http.request",
                extra=extra,
            )
        elif isinstance(failure, Exception):
            _LOG.error("http.request", extra=extra, exc_info=failure)
        else:  # cancellation or interpreter exit, for example a client dropping an SSE stream
            extra["status"] = status
            extra["error_type"] = type(failure).__name__
            _LOG.info("http.request", extra=extra)
