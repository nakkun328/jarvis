"""Passphrase hashing with scrypt from the standard library.

Encoded format (self-describing, ``$``-separated)::

    scrypt$<N>$<r>$<p>$<salt, base64url no padding>$<hash, base64url no padding>

Passphrases are NFKC-normalized before use so that the same typed text hashes identically
whatever the input method produced. Verification never raises: any malformed or hostile input
simply fails to verify.
"""

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import unicodedata

SCHEME = "scrypt"
MIN_PASSPHRASE_CHARS = 12
MAX_PASSPHRASE_CHARS = 1024

DEFAULT_N = 2**15
DEFAULT_R = 8
DEFAULT_P = 3
SALT_BYTES = 16
HASH_BYTES = 32

# Bounds applied when *reading* a stored hash, so a corrupted or hostile value cannot make the
# server allocate unbounded memory or burn unbounded CPU.
_MIN_N, _MAX_N = 2**4, 2**20
_MAX_R, _MAX_P = 32, 16
_MAX_MEMORY_BYTES = 256 * 1024 * 1024
_MIN_SALT, _MAX_SALT = 8, 64
_MIN_HASH, _MAX_HASH = 16, 64

_B64 = re.compile(r"[A-Za-z0-9_-]+")
_INT = re.compile(r"[0-9]{1,8}")


class PasswordHashError(ValueError):
    """The passphrase or the encoded hash is not acceptable."""


def normalize(passphrase: str) -> str:
    return unicodedata.normalize("NFKC", passphrase)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    if not _B64.fullmatch(text):
        raise PasswordHashError("invalid encoding")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise PasswordHashError("invalid encoding") from None


def _maxmem(n: int, r: int, p: int) -> int:
    return 128 * r * (n + p + 2) + 1024 * 1024


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(
        normalize(passphrase).encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=_maxmem(n, r, p),
        dklen=length,
    )


def parse_hash(encoded: str) -> tuple[int, int, int, bytes, bytes]:
    """Return ``(n, r, p, salt, digest)`` or raise PasswordHashError without echoing input."""
    if not isinstance(encoded, str) or len(encoded) > 512:
        raise PasswordHashError("invalid hash")
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        raise PasswordHashError("invalid hash")
    if not all(_INT.fullmatch(part) for part in parts[1:4]):
        raise PasswordHashError("invalid hash")
    n, r, p = (int(part) for part in parts[1:4])
    if n < _MIN_N or n > _MAX_N or n & (n - 1):
        raise PasswordHashError("invalid hash")
    if not 1 <= r <= _MAX_R or not 1 <= p <= _MAX_P or 128 * r * n > _MAX_MEMORY_BYTES:
        raise PasswordHashError("invalid hash")
    salt, digest = _b64decode(parts[4]), _b64decode(parts[5])
    if not _MIN_SALT <= len(salt) <= _MAX_SALT or not _MIN_HASH <= len(digest) <= _MAX_HASH:
        raise PasswordHashError("invalid hash")
    return n, r, p, salt, digest


def check_passphrase_policy(passphrase: str) -> None:
    text = normalize(passphrase)
    if len(text) < MIN_PASSPHRASE_CHARS:
        raise PasswordHashError(f"passphrase must be at least {MIN_PASSPHRASE_CHARS} characters")
    if len(text) > MAX_PASSPHRASE_CHARS:
        raise PasswordHashError(f"passphrase must be at most {MAX_PASSPHRASE_CHARS} characters")
    if "\x00" in text:
        raise PasswordHashError("passphrase must not contain null characters")


def hash_passphrase(
    passphrase: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P
) -> str:
    """Return the encoded scrypt hash of ``passphrase`` with a fresh random salt."""
    check_passphrase_policy(passphrase)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(passphrase, salt, n, r, p, HASH_BYTES)
    encoded = f"{SCHEME}${n}${r}${p}${_b64encode(salt)}${_b64encode(digest)}"
    parse_hash(encoded)  # refuse parameters this module would not accept when reading
    return encoded


def verify_passphrase(passphrase: str, encoded: str) -> bool:
    """Constant-time check of ``passphrase`` against ``encoded``. Returns False on any problem."""
    try:
        n, r, p, salt, expected = parse_hash(encoded)
        if not isinstance(passphrase, str) or len(passphrase) > MAX_PASSPHRASE_CHARS:
            return False
        actual = _derive(passphrase, salt, n, r, p, len(expected))
    except (PasswordHashError, ValueError, MemoryError, OverflowError):
        return False
    return hmac.compare_digest(actual, expected)
