"""Pure ASGI enforcement: sessions, same-origin checks, and security headers.

Only installed when authentication is enabled. Decisions are made from the method and the
decoded request path, deny by default: anything not in the small public allowlist needs a valid
session. Responses never say *why* a session was refused.
"""

from urllib.parse import urlsplit

from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.auth.service import AuthService

LOGIN_PAGE = "/login"
LOGIN_ASSETS = frozenset({"/static/login.css", "/static/login.js"})
LOGIN_API = "/api/auth/login"
STATUS_API = "/api/auth/status"
LIVENESS = "/health/live"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORTS = {"http": 80, "https": 443}

SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
)


def is_public(method: str, path: str) -> bool:
    """The only requests served without a session."""
    if method in ("GET", "HEAD"):
        return path in (LOGIN_PAGE, LIVENESS, STATUS_API) or path in LOGIN_ASSETS
    return method == "POST" and path == LOGIN_API


def _single_header(scope: Scope, name: bytes) -> str | None:
    values = [v for k, v in scope.get("headers", ()) if k == name]
    if len(values) != 1:
        return None
    return values[0].decode("latin-1")


def _split_host(value: str, scheme: str) -> tuple[str, int] | None:
    """Parse ``host[:port]`` (bracketed IPv6 allowed) into a comparable pair."""
    try:
        parsed = urlsplit(f"//{value}")
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if not host or parsed.username is not None or parsed.path or parsed.query or parsed.fragment:
        return None
    return host.lower(), port if port is not None else _DEFAULT_PORTS[scheme]


def is_same_origin(scope: Scope, trusted_proxy: bool) -> bool:
    """Origin (else Referer) must name the host the request was addressed to."""
    origin = _single_header(scope, b"origin")
    if origin is None and not any(k == b"origin" for k, _ in scope.get("headers", ())):
        origin = _single_header(scope, b"referer")
    if origin is None:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parsed.netloc:
        return False
    theirs = _split_host(parsed.netloc, scheme)
    host_value = _single_header(scope, b"host")
    if trusted_proxy and (forwarded := _single_header(scope, b"x-forwarded-host")):
        host_value = forwarded
    if theirs is None or host_value is None:
        return False
    # A Host header carries no scheme; assume it uses the default port of the caller's scheme.
    ours = _split_host(host_value, scheme)
    return ours is not None and ours == theirs


def _is_resource(path: str) -> bool:
    return path.startswith(("/api/", "/static/", "/health/"))


class AuthMiddleware:
    def __init__(self, app: ASGIApp, auth: AuthService) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "websocket":
            if not self.auth.is_authenticated(scope):
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        if kind != "http":
            await self.app(scope, receive, send)
            return

        method, path = scope["method"].upper(), scope["path"]
        refusal: Response | None = None
        if not is_public(method, path) and not self.auth.is_authenticated(scope):
            if _is_resource(path) or method not in ("GET", "HEAD"):
                refusal = JSONResponse({"detail": "unauthorized"}, status_code=401)
            else:
                refusal = RedirectResponse(LOGIN_PAGE, status_code=303)
        elif method not in _SAFE_METHODS and not is_same_origin(scope, self.auth.trusted_proxy):
            refusal = JSONResponse({"detail": "forbidden"}, status_code=403)

        async def send_hardened(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                present = {name.lower() for name, _ in headers}
                headers.extend(h for h in SECURITY_HEADERS if h[0] not in present)
                if b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        if refusal is not None:
            await refusal(scope, receive, send_hardened)
            return
        await self.app(scope, receive, send_hardened)
