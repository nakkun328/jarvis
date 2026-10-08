"""Authentication service shared by the middleware and the login routes."""

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from starlette.types import Scope

from backend.auth.limiter import LoginLimiter
from backend.auth.passwords import verify_passphrase
from backend.auth.session import SessionSigner
from backend.core.config import Settings

SESSION_COOKIE = "jarvis_session"
SECURE_SESSION_COOKIE = "__Host-jarvis_session"
_MAX_COOKIE_HEADER = 8192
_MAX_CONCURRENT_HASHES = 2

_LOG = logging.getLogger("jarvis.auth")


@dataclass(frozen=True)
class AuthService:
    """Immutable configuration plus the limiter's mutable state."""

    enabled: bool
    passphrase_hash: str | None = None
    signer: SessionSigner | None = None
    limiter: LoginLimiter | None = None
    cookie_secure: bool = True
    trusted_proxy: bool = False
    ephemeral_key: bool = False
    hash_slots: asyncio.Semaphore | None = None

    @classmethod
    def disabled(cls) -> "AuthService":
        return cls(enabled=False)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.time,
        limiter: LoginLimiter | None = None,
    ) -> "AuthService":
        if settings.auth_passphrase_hash is None:
            return cls.disabled()
        signing_key = settings.auth_signing_key
        ephemeral = signing_key is None
        if signing_key is None:
            signing_key = secrets.token_urlsafe(48)
        return cls(
            enabled=True,
            passphrase_hash=settings.auth_passphrase_hash,
            signer=SessionSigner(
                signing_key,
                settings.auth_passphrase_hash,
                settings.auth_session_hours * 3600,
                clock,
            ),
            limiter=limiter or LoginLimiter(),
            cookie_secure=settings.auth_cookie_secure,
            trusted_proxy=settings.trusted_proxy,
            ephemeral_key=ephemeral,
            hash_slots=asyncio.Semaphore(_MAX_CONCURRENT_HASHES),
        )

    # --- startup -------------------------------------------------------------------------

    def log_startup(self) -> None:
        if not self.enabled:
            return  # default local mode: keep startup output unchanged
        _LOG.info("auth.enabled")
        if self.ephemeral_key:
            _LOG.warning("auth.signing_key.ephemeral: sessions reset on every restart")
        if not self.cookie_secure:
            _LOG.warning("auth.cookie.insecure: use only on loopback")

    # --- cookies and sessions ------------------------------------------------------------

    @property
    def cookie_name(self) -> str:
        return SECURE_SESSION_COOKIE if self.cookie_secure else SESSION_COOKIE

    def is_authenticated(self, scope: Scope) -> bool:
        """True when the request carries a valid session cookie. Never raises."""
        if not self.enabled or self.signer is None:
            return False
        try:
            return self._authenticated(scope)
        except Exception:
            return False

    def _authenticated(self, scope: Scope) -> bool:
        assert self.signer is not None
        wanted = self.cookie_name
        for name, value in scope.get("headers", ()):
            if name != b"cookie" or len(value) > _MAX_COOKIE_HEADER:
                continue
            for pair in value.decode("latin-1").split(";"):
                key, sep, token = pair.strip().partition("=")
                if sep and key == wanted and self.signer.verify(token):
                    return True
        return False

    def issue_cookie_value(self) -> tuple[str, int]:
        assert self.signer is not None
        return self.signer.issue(), self.signer.lifetime_seconds

    def check_passphrase(self, passphrase: str) -> bool:
        assert self.passphrase_hash is not None
        return verify_passphrase(passphrase, self.passphrase_hash)

    # --- client identity -----------------------------------------------------------------

    def client_address(self, scope: Scope) -> str:
        """The direct peer, or the last X-Forwarded-For entry when one proxy is trusted.

        IPv6 clients are grouped by /64 because one host usually controls the whole prefix.
        """
        client = scope.get("client")
        address = str(client[0]) if client else "unknown"
        if self.trusted_proxy:
            forwarded = b",".join(v for k, v in scope.get("headers", ()) if k == b"x-forwarded-for")
            address = forwarded.decode("latin-1").split(",")[-1].strip() or address
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return str(client[0]) if client else "unknown"
        if isinstance(parsed, ipaddress.IPv6Address):
            if parsed.ipv4_mapped is not None:
                return str(parsed.ipv4_mapped)
            return str(ipaddress.ip_network(f"{parsed}/64", strict=False))  # one host, many /128s
        return str(parsed)

    def client_id(self, scope: Scope) -> str:
        """Stable keyed digest of the client address, safe to write to logs."""
        assert self.signer is not None
        address = self.client_address(scope)
        return hmac.new(self.signer.log_key, address.encode("utf-8"), hashlib.sha256).hexdigest()[
            :12
        ]

    def log(self, event: str, scope: Scope) -> None:
        if not re.fullmatch(r"auth\.[a-z_.]+", event):
            raise ValueError("auth events use fixed names")
        _LOG.info(event, extra={"client": self.client_id(scope)})
