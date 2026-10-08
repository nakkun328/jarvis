"""Login, logout and status endpoints, plus the login page."""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response

from backend.auth.passwords import MAX_PASSPHRASE_CHARS, normalize
from backend.auth.service import AuthService

MAX_BODY_BYTES = 4096

_LOGIN_PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


class _TooLarge(Exception):
    pass


def _json(
    status: int, body: dict[str, object], headers: dict[str, str] | None = None
) -> JSONResponse:
    merged = {"Cache-Control": "no-store", **(headers or {})}
    return JSONResponse(body, status_code=status, headers=merged)


async def _read_passphrase(request: Request) -> str | None:
    """Return the submitted passphrase, or None for any malformed request."""
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "application/json":
        return None
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        raise _TooLarge
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise _TooLarge
    try:
        data = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    passphrase = data.get("passphrase")
    if not isinstance(passphrase, str) or len(passphrase) > MAX_PASSPHRASE_CHARS:
        return None
    return passphrase


def create_auth_router(auth: AuthService, frontend_dir: Path | None) -> APIRouter:
    router = APIRouter()
    limiter, slots = auth.limiter, auth.hash_slots
    assert limiter is not None and slots is not None

    @router.post("/api/auth/login")
    async def login(request: Request) -> Response:
        scope = request.scope
        try:
            passphrase = await _read_passphrase(request)
        except _TooLarge:
            return _json(413, {"detail": "request too large"})
        if passphrase is None:
            return _json(400, {"detail": "invalid request"})
        client = auth.client_address(scope)
        # begin() counts this attempt as a failure at once (no await between the check and the
        # count), so parallel guesses cannot outrun the limit; a correct passphrase refunds it.
        decision = limiter.begin(client)
        if not decision.allowed:
            auth.log("auth.login.locked", scope)
            return _json(
                429, {"detail": "too many attempts"}, {"Retry-After": str(decision.retry_after)}
            )
        async with slots:
            matched = await run_in_threadpool(auth.check_passphrase, normalize(passphrase))
        if not matched:
            auth.log("auth.login.failure", scope)
            await asyncio.sleep(limiter.failure_delay)
            return _json(401, {"detail": "invalid credentials"})
        limiter.succeeded(client)
        auth.log("auth.login.success", scope)
        value, max_age = auth.issue_cookie_value()
        response = _json(200, {"authenticated": True})
        response.set_cookie(
            auth.cookie_name,
            value,
            max_age=max_age,
            path="/",
            secure=auth.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @router.post("/api/auth/logout")
    async def logout(request: Request) -> Response:
        auth.log("auth.logout", request.scope)
        response = _json(200, {"authenticated": False})
        response.delete_cookie(
            auth.cookie_name,
            path="/",
            secure=auth.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @router.get("/api/auth/status")
    async def status(request: Request) -> Response:
        return _json(200, {"authenticated": auth.is_authenticated(request.scope)})

    if frontend_dir is not None and (frontend_dir / "login.html").is_file():

        @router.get("/login", include_in_schema=False)
        def login_page() -> FileResponse:
            return FileResponse(
                frontend_dir / "login.html",
                headers={"Cache-Control": "no-store", "Content-Security-Policy": _LOGIN_PAGE_CSP},
            )

    return router
