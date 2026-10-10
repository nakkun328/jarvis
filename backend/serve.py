"""Supported way to start JARVIS: ``python -m backend.serve [--host HOST] [--port PORT]``.

Binding anything other than a loopback address is refused unless the login layer is enabled
(``JARVIS_AUTH_PASSPHRASE_HASH``) and session cookies are marked Secure. JARVIS does not speak
TLS itself: for remote use, bind to loopback and put a TLS-terminating reverse proxy or tunnel in
front (Tailscale Serve, Cloudflare Tunnel, Caddy, ...). See docs/auth.md.
"""

import argparse
import ipaddress
import sys
from collections.abc import Sequence

from backend.core.config import ConfigError, Settings


class StartupRefused(RuntimeError):
    """The requested bind address is not allowed with the current configuration."""


def is_loopback_host(host: str) -> bool:
    """True only for ``localhost`` and loopback IP literals (including IPv4-mapped ones)."""
    candidate = host.strip().lower()
    if candidate == "localhost":
        return True
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False  # any other name could resolve to a public interface
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def check_bind(host: str, settings: Settings) -> None:
    """Raise StartupRefused unless ``host`` may be served with ``settings``."""
    if is_loopback_host(host):
        return
    if not settings.auth_enabled:
        raise StartupRefused(
            "Refusing to bind a non-loopback address without authentication. "
            "Set JARVIS_AUTH_PASSPHRASE_HASH (see docs/auth.md) or bind 127.0.0.1."
        )
    if not settings.auth_cookie_secure:
        raise StartupRefused(
            "Refusing to bind a non-loopback address with JARVIS_AUTH_COOKIE_SECURE=false. "
            "Insecure cookies are only for loopback development."
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the JARVIS server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
        check_bind(args.host, settings)
    except (ConfigError, StartupRefused) as error:
        print(f"jarvis: {error}", file=sys.stderr)
        return 2

    import uvicorn

    from backend.api.app import create_app

    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        # Forwarded headers are honored only through JARVIS_TRUSTED_PROXY, in one place.
        proxy_headers=False,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
