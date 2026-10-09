"""Read-only Devices endpoint: what the server can truthfully know about this connection.

There is no device registry and no per-session storage (sessions are stateless signed cookies),
so a list of registered devices or a last-connection time per device does not exist. The answer
therefore has three parts: the device making this request (summarised from its User-Agent into
browser and OS families, never the raw string, never an address), the server's configuration
(provider names and feature switches only, never keys or paths), and an explicit
"registered devices: unavailable" block. See docs/devices.md.
"""

import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.core.config import Settings

_NO_STORE = {"Cache-Control": "no-store"}
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

# Order matters: Edge and Opera also say Chrome, Chrome also says Safari.
_BROWSERS = (
    ("Edge", re.compile(r"Edg(?:e|A|iOS)?/")),
    ("Opera", re.compile(r"OPR/|Opera")),
    ("Firefox", re.compile(r"Firefox/|FxiOS/")),
    ("Chrome", re.compile(r"Chrome/|CriOS/")),
    ("Safari", re.compile(r"Safari/")),
)
# iPhone/iPad before macOS (their UA says "like Mac OS X"); Android before Linux.
_SYSTEMS = (
    ("iOS", re.compile(r"iPhone|iPad|iPod")),
    ("Android", re.compile(r"Android")),
    ("Windows", re.compile(r"Windows")),
    ("macOS", re.compile(r"Macintosh|Mac OS X")),
    ("ChromeOS", re.compile(r"CrOS")),
    ("Linux", re.compile(r"Linux|X11")),
)
_MAX_UA = 512


def summarize_user_agent(value: str | None) -> dict[str, str]:
    """Browser and OS family names from a User-Agent; "unknown" when nothing matches."""
    text = (value or "")[:_MAX_UA]
    browser = next((name for name, rx in _BROWSERS if rx.search(text)), "unknown")
    system = next((name for name, rx in _SYSTEMS if rx.search(text)), "unknown")
    return {"browser": browser, "os": system}


def connection_kind(request: Request, trusted_proxy: bool) -> str:
    """local, remote or proxied (behind a trusted proxy the peer address says nothing)."""
    if trusted_proxy:
        return "proxied"
    host = request.client.host if request.client else ""
    return "local" if host in _LOOPBACK else "remote"


def create_devices_router(
    settings: Settings, *, login_enabled: bool, trusted_proxy: bool = False
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/devices")
    def devices(request: Request) -> JSONResponse:
        body: dict[str, Any] = {
            "current": {
                **summarize_user_agent(request.headers.get("user-agent")),
                "connection": connection_kind(request, trusted_proxy),
                "scheme": request.url.scheme,
            },
            "server": {
                "login_enabled": login_enabled,
                "providers": {
                    "chat": settings.llm_provider,
                    "search": settings.search_provider,
                },
                "features": {
                    "router": settings.router,
                    "research": settings.research_enabled,
                    "shell": settings.shell_enabled,
                    "model_choices": bool(settings.model_choices),
                },
            },
            "registered_devices": {
                "available": False,
                "reason": "no_device_registry",
                "devices": [],
            },
        }
        return JSONResponse(body, headers=_NO_STORE)

    return router
