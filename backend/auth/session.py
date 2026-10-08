"""Stateless signed session tokens.

Token: ``<base64url(payload JSON)>.<base64url(HMAC-SHA256)>`` with payload
``{"iat": issued_at, "exp": expires_at, "nonce": random}`` (Unix seconds). The MAC key is derived
from the signing key *and* the stored passphrase hash, so changing either invalidates every
session. Lifetime is absolute: there is no refresh and no sliding window. Verification returns a
plain bool so callers cannot leak why a token was refused.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable

_B64 = re.compile(r"[A-Za-z0-9_-]+")
_MAX_TOKEN_CHARS = 512
_CLOCK_SKEW_SECONDS = 5
_KEY_CONTEXT = b"jarvis-session-v1\0"
_LOG_CONTEXT = b"jarvis-log-id-v1\0"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes | None:
    if not _B64.fullmatch(text):
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None


def derive_key(signing_key: str, passphrase_hash: str, context: bytes) -> bytes:
    return hmac.new(
        signing_key.encode("utf-8"), context + passphrase_hash.encode("utf-8"), hashlib.sha256
    ).digest()


class SessionSigner:
    def __init__(
        self,
        signing_key: str,
        passphrase_hash: str,
        lifetime_seconds: int,
        clock: Callable[[], float],
    ) -> None:
        if lifetime_seconds <= 0:
            raise ValueError("lifetime must be positive")
        self._key = derive_key(signing_key, passphrase_hash, _KEY_CONTEXT)
        self.log_key = derive_key(signing_key, passphrase_hash, _LOG_CONTEXT)
        self.lifetime_seconds = lifetime_seconds
        self._clock = clock

    def _mac(self, payload: str) -> bytes:
        return hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest()

    def issue(self) -> str:
        now = int(self._clock())
        return self.sign(
            {"iat": now, "exp": now + self.lifetime_seconds, "nonce": secrets.token_hex(16)}
        )

    def sign(self, claims: dict[str, object]) -> str:
        payload = _b64encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        return f"{payload}.{_b64encode(self._mac(payload))}"

    def verify(self, token: object) -> bool:
        try:
            return self._verify(token)
        except Exception:
            return False

    def _verify(self, token: object) -> bool:
        if not isinstance(token, str) or not 0 < len(token) <= _MAX_TOKEN_CHARS:
            return False
        parts = token.split(".")
        if len(parts) != 2:
            return False
        payload, signature = parts
        provided = _b64decode(signature)
        if provided is None or not hmac.compare_digest(provided, self._mac(payload)):
            return False
        raw = _b64decode(payload)
        if raw is None:
            return False
        claims = json.loads(raw)
        if not isinstance(claims, dict):
            return False
        issued, expires, nonce = claims.get("iat"), claims.get("exp"), claims.get("nonce")
        for value in (issued, expires):
            if not isinstance(value, int) or isinstance(value, bool):
                return False
        if not isinstance(nonce, str) or not nonce:
            return False
        now = self._clock()
        if issued > now + _CLOCK_SKEW_SECONDS:  # future-dated
            return False
        if expires <= issued or expires - issued > self.lifetime_seconds:
            return False
        return now < expires
