"""Safe web page reader (JAR-38) with an injectable transport and DNS resolver.

Everything fetched here is untrusted data. The reader validates every URL (including each
redirect target), resolves the host itself, refuses non-public addresses, and then asks the
transport to connect to the validated address, so the connection cannot be redirected to a
different address by a second DNS lookup. Failures are reported as fixed ``ReadFailure`` codes;
upstream messages, headers, and bodies are never put into exceptions or logs.
"""

import asyncio
import codecs
import hashlib
import ipaddress
import logging
import re
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from backend.research.extraction import ExtractionError, extract_page

logger = logging.getLogger(__name__)

USER_AGENT = "JarvisResearchReader/0.1 (personal research assistant)"
ALLOWED_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain", "application/json"}
)
# The only response headers the reader ever looks at; transports must drop everything else.
RESPONSE_HEADERS = frozenset({"content-type", "content-length", "content-encoding", "location"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_URL_LENGTH = 2048

BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # carrier-grade NAT
        "127.0.0.0/8",
        "169.254.0.0/16",  # link local, includes cloud metadata 169.254.169.254
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/96",  # unspecified and deprecated IPv4-compatible addresses
        "::1/128",
        "64:ff9b::/96",  # NAT64 can reach IPv4 private space
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/32",  # Teredo
        "2001:db8::/32",
        "2002::/16",  # 6to4
        "fc00::/7",  # unique local, includes fd00:ec2::254 metadata
        "fe80::/10",
        "ff00::/8",
    )
)
BLOCKED_HOST_NAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})
BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
    ".lan",
    ".intranet",
)
# inet_aton style literals ("2130706433", "0x7f.1", "017700000001") that resolvers accept.
_LEGACY_NUMERIC_HOST = re.compile(r"^(?:0x[0-9a-f]*|\d+)(?:\.(?:0x[0-9a-f]*|\d+)){0,3}$", re.I)
UNSAFE_CODECS = frozenset({"utf-7", "punycode", "idna", "undefined"})
_CHARSET_PARAM = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9_.:-]+)", re.I)
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)


class ReadFailure(StrEnum):
    BLOCKED_SCHEME = "blocked_scheme"  # not a well-formed absolute http(s) URL without userinfo
    BLOCKED_HOST = "blocked_host"
    REDIRECT_LIMIT = "redirect_limit"
    TOO_LARGE = "too_large"
    TIMEOUT = "timeout"
    BAD_CONTENT_TYPE = "bad_content_type"
    HTTP_ERROR = "http_error"
    EXTRACTION_FAILED = "extraction_failed"
    NETWORK_ERROR = "network_error"


class ReaderError(Exception):
    """A page could not be read; only the fixed reason code is carried."""

    def __init__(self, reason: ReadFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    retrieved_at: datetime
    body_sha256: str
    text: str
    title: str | None
    author: str | None
    published_at: datetime | None
    language: str | None
    truncated: bool


@dataclass(frozen=True)
class ReaderPolicy:
    max_redirects: int = 5
    max_body_bytes: int = 2 * 1024 * 1024
    hard_max_content_length: int = 16 * 1024 * 1024
    request_timeout: float = 10.0
    total_timeout: float = 20.0
    max_text_chars: int = 20_000
    allowed_ports: frozenset[int] = frozenset({80, 443})
    max_address_attempts: int = 3


@dataclass(frozen=True)
class TransportRequest:
    url: str  # validated URL, used for the Host header and TLS server name
    address: str  # validated public IP address the transport must connect to
    headers: Mapping[str, str]
    max_bytes: int
    timeout: float


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str]  # lower-case names, only RESPONSE_HEADERS
    body: bytes  # at most ``max_bytes``; empty for redirects
    truncated: bool


class PageTransport(Protocol):
    """One GET without redirects. Raise ``ReaderError`` for failures, with no upstream detail."""

    async def fetch(self, request: TransportRequest) -> TransportResponse: ...


class Resolver(Protocol):
    async def __call__(self, host: str) -> Sequence[str]: ...


@dataclass(frozen=True)
class UrlTarget:
    url: str
    scheme: str
    host: str  # normalised ASCII host, no brackets, no trailing dot
    port: int
    literal_address: str | None = field(default=None)


def is_blocked_address(address: str) -> bool:
    """True unless ``address`` is a plain public unicast IP address."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if getattr(ip, "scope_id", None):
        return True
    if any(ip in network for network in BLOCKED_NETWORKS):
        return True
    return not ip.is_global or ip.is_multicast


def validate_url(url: str, policy: ReaderPolicy | None = None) -> UrlTarget:
    """Check scheme, userinfo, port, and host spelling. Does not resolve DNS."""
    policy = policy or ReaderPolicy()
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH or url != url.strip():
        raise ReaderError(ReadFailure.BLOCKED_SCHEME)
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in url):
        raise ReaderError(ReadFailure.BLOCKED_SCHEME)
    try:
        parts = urlsplit(url)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        raise ReaderError(ReadFailure.BLOCKED_SCHEME) from None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        raise ReaderError(ReadFailure.BLOCKED_SCHEME)
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise ReaderError(ReadFailure.BLOCKED_SCHEME)
    if port is None:
        port = 443 if scheme == "https" else 80
    if port not in policy.allowed_ports:
        raise ReaderError(ReadFailure.BLOCKED_HOST)
    host = _normalise_host(hostname)
    literal = _literal_address(host)
    if literal is not None and is_blocked_address(literal):
        raise ReaderError(ReadFailure.BLOCKED_HOST)
    if literal is None and _is_blocked_name(host):
        raise ReaderError(ReadFailure.BLOCKED_HOST)
    return UrlTarget(url=url, scheme=scheme, host=host, port=port, literal_address=literal)


def _normalise_host(hostname: str) -> str:
    host = hostname.rstrip(".")
    if not host or "%" in host:
        raise ReaderError(ReadFailure.BLOCKED_HOST)
    try:
        # NFKC-style IDNA mapping turns full-width digits and dots into their ASCII forms.
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ReaderError(ReadFailure.BLOCKED_HOST) from None


def _literal_address(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if ":" in host or _LEGACY_NUMERIC_HOST.match(host):
        # Not a canonical literal, but something a resolver or browser may read as an address.
        raise ReaderError(ReadFailure.BLOCKED_HOST)
    return None


def _is_blocked_name(host: str) -> bool:
    return host in BLOCKED_HOST_NAMES or host.endswith(BLOCKED_HOST_SUFFIXES) or "." not in host


async def system_resolver(host: str) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class HttpxTransport:
    """Real transport. Connects to the validated IP and keeps the original Host and TLS name."""

    def __init__(self, client_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client_transport = client_transport

    async def fetch(self, request: TransportRequest) -> TransportResponse:
        original = httpx.URL(request.url)
        host = original.raw_host.decode("ascii")
        host_header = host if ":" not in host else f"[{host}]"
        if original.port is not None:
            host_header = f"{host_header}:{original.port}"
        headers = {**request.headers, "Host": host_header}
        extensions = {"sni_hostname": host} if original.scheme == "https" else {}
        target = original.copy_with(host=request.address)
        try:
            async with httpx.AsyncClient(
                transport=self._client_transport,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(request.timeout),
            ) as client:
                outgoing = client.build_request(
                    "GET", target, headers=headers, extensions=extensions
                )
                response = await client.send(outgoing, stream=True)
                try:
                    return await _read_response(response, request.max_bytes)
                finally:
                    await response.aclose()
        except ReaderError:
            raise
        except httpx.TimeoutException:
            raise ReaderError(ReadFailure.TIMEOUT) from None
        except (httpx.HTTPError, OSError, ValueError):
            raise ReaderError(ReadFailure.NETWORK_ERROR) from None


async def _read_response(response: httpx.Response, max_bytes: int) -> TransportResponse:
    headers = {
        name.lower(): value
        for name, value in response.headers.items()
        if name.lower() in RESPONSE_HEADERS
    }
    if response.status_code in REDIRECT_STATUSES:
        return TransportResponse(response.status_code, headers, b"", False)
    if response.is_stream_consumed:  # fully buffered by an in-memory test transport
        return TransportResponse(
            response.status_code,
            headers,
            response.content[:max_bytes],
            len(response.content) > max_bytes,
        )
    body = bytearray()
    truncated = False
    # Raw bytes on purpose: the reader refuses content-encoding, so nothing can expand.
    async for chunk in response.aiter_raw():
        room = max_bytes - len(body)
        if len(chunk) > room:
            body += chunk[:room]
            truncated = True
            break
        body += chunk
    return TransportResponse(response.status_code, headers, bytes(body), truncated)


class PageReader:
    def __init__(
        self,
        transport: PageTransport | None = None,
        resolver: Resolver | None = None,
        policy: ReaderPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport: PageTransport = transport or HttpxTransport()
        self._resolver: Resolver = resolver or system_resolver
        self._policy = policy or ReaderPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def read(self, url: str) -> FetchedPage:
        try:
            async with asyncio.timeout(self._policy.total_timeout):
                return await self._read(url)
        except TimeoutError:
            reason = ReadFailure.TIMEOUT
        except ReaderError as error:
            reason = error.reason
        logger.info("research page read failed reason=%s", reason.value)
        raise ReaderError(reason)

    async def _read(self, url: str) -> FetchedPage:
        policy = self._policy
        current = url
        for hop in range(policy.max_redirects + 1):
            target = validate_url(current, policy)
            response = await self._fetch_hop(target)
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("location", "").strip()
                if not location:
                    raise ReaderError(ReadFailure.HTTP_ERROR)
                if hop == policy.max_redirects:
                    raise ReaderError(ReadFailure.REDIRECT_LIMIT)
                current = urldefrag(urljoin(current, location))[0]
                continue
            return self._build_page(url, urldefrag(current)[0], response)
        raise ReaderError(ReadFailure.REDIRECT_LIMIT)  # pragma: no cover

    async def _fetch_hop(self, target: UrlTarget) -> TransportResponse:
        policy = self._policy
        addresses = await self._public_addresses(target)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9",
            "Accept-Encoding": "identity",
        }
        last: ReaderError | None = None
        for address in addresses[: policy.max_address_attempts]:
            request = TransportRequest(
                url=target.url,
                address=address,
                headers=headers,
                max_bytes=policy.max_body_bytes,
                timeout=policy.request_timeout,
            )
            try:
                return await self._transport.fetch(request)
            except ReaderError as error:
                if error.reason is not ReadFailure.NETWORK_ERROR:
                    raise
                last = error
            except TimeoutError:
                raise ReaderError(ReadFailure.TIMEOUT) from None
            except Exception:
                last = ReaderError(ReadFailure.NETWORK_ERROR)
        raise last or ReaderError(ReadFailure.NETWORK_ERROR)

    async def _public_addresses(self, target: UrlTarget) -> list[str]:
        if target.literal_address is not None:
            return [target.literal_address]
        try:
            resolved = [str(address) for address in await self._resolver(target.host)]
        except TimeoutError:
            raise ReaderError(ReadFailure.TIMEOUT) from None
        except Exception:
            raise ReaderError(ReadFailure.NETWORK_ERROR) from None
        if not resolved:
            raise ReaderError(ReadFailure.NETWORK_ERROR)
        # One private answer taints the whole name: a rebinding resolver may mix answers.
        if any(is_blocked_address(address) for address in resolved):
            raise ReaderError(ReadFailure.BLOCKED_HOST)
        return resolved

    def _build_page(
        self, requested_url: str, final_url: str, response: TransportResponse
    ) -> FetchedPage:
        if response.status_code != 200:  # 204 and 206 carry no complete document
            raise ReaderError(ReadFailure.HTTP_ERROR)
        headers = response.headers
        encoding = headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"", "identity"}:
            raise ReaderError(ReadFailure.BAD_CONTENT_TYPE)
        content_type = headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type not in ALLOWED_CONTENT_TYPES:
            raise ReaderError(ReadFailure.BAD_CONTENT_TYPE)
        declared = headers.get("content-length", "").strip()
        if declared.isdigit() and int(declared) > self._policy.hard_max_content_length:
            raise ReaderError(ReadFailure.TOO_LARGE)
        body = response.body[: self._policy.max_body_bytes]
        if _looks_binary(body):
            raise ReaderError(ReadFailure.BAD_CONTENT_TYPE)
        content = _decode(body, content_type, final=not response.truncated)
        try:
            extracted = extract_page(content, media_type, max_chars=self._policy.max_text_chars)
        except ExtractionError:
            raise ReaderError(ReadFailure.EXTRACTION_FAILED) from None
        return FetchedPage(
            requested_url=requested_url,
            final_url=final_url,
            status_code=response.status_code,
            content_type=media_type,
            retrieved_at=self._clock().astimezone(UTC),
            body_sha256=hashlib.sha256(body).hexdigest(),
            text=extracted.text,
            title=extracted.title,
            author=extracted.author,
            published_at=extracted.published_at,
            language=extracted.language,
            truncated=response.truncated or extracted.truncated,
        )


def _looks_binary(body: bytes) -> bool:
    head = body[:1024]
    return head.startswith(b"%PDF-") or (b"\x00" in head and not _has_utf16_bom(head))


def _has_utf16_bom(head: bytes) -> bool:
    return head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))


def _decode(body: bytes, content_type: str, *, final: bool) -> str:
    """Decode with a declared or sniffed charset; invalid bytes become U+FFFD."""
    label = "utf-8"
    if body.startswith(codecs.BOM_UTF8):
        label = "utf-8-sig"
    elif _has_utf16_bom(body):
        label = "utf-16"
    else:
        match = _CHARSET_PARAM.search(content_type)
        if match:
            label = match.group(1)
        else:
            sniffed = _META_CHARSET.search(body[:4096])
            if sniffed:
                label = sniffed.group(1).decode("ascii", "ignore")
    for candidate in (label, "utf-8"):
        try:
            if codecs.lookup(candidate).name in UNSAFE_CODECS:
                continue
            decoder = codecs.getincrementaldecoder(candidate)(errors="replace")
            text = decoder.decode(body, final=final)
        except Exception:  # unknown label, text transform such as rot13, or decoder error
            continue
        if isinstance(text, str):
            return text
    return body.decode("utf-8", errors="replace")
