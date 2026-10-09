"""Which hits to read: no blocked URLs, and one registrable domain should not fill every slot.

``registrable_domain`` is a small heuristic (last two labels, three for the common
two-level public suffixes such as ``co.jp``); it does not ship the full public suffix list.
A wrong guess only changes which hits are read first or which sources count as independent,
never what is stored or cited.
"""

from collections.abc import Sequence
from urllib.parse import urlsplit

from backend.research.reader import ReaderError, ReadFailure, validate_url
from backend.research.search import SearchResult

_SECOND_LEVEL = frozenset({"co", "com", "ne", "or", "ac", "go", "ed", "gr", "org", "net", "gov"})
_TWO_LEVEL_TLDS = frozenset(
    {"jp", "uk", "au", "nz", "kr", "cn", "br", "in", "za", "tw", "hk", "sg"}
)


def registrable_domain(url: str) -> str | None:
    """Lower-case registrable domain of a URL (an IP literal is its own domain), else None."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".").lower()
    labels = host.split(".")
    if len(labels) <= 2 or ":" in host or all(label.isdigit() for label in labels):
        return host
    if labels[-1] in _TWO_LEVEL_TLDS and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def blocked_reason(url: str) -> ReadFailure | None:
    """The reader's own verdict on the URL spelling (no DNS); None when it may be fetched."""
    try:
        validate_url(url)
    except ReaderError as error:
        return error.reason
    return None


def split_blocked(
    hits: Sequence[SearchResult],
) -> tuple[list[SearchResult], list[tuple[SearchResult, ReadFailure]]]:
    """(hits that may be fetched, blocked hits with their code); order is kept."""
    ok: list[SearchResult] = []
    blocked: list[tuple[SearchResult, ReadFailure]] = []
    for hit in hits:
        reason = blocked_reason(hit.url)
        if reason is None:
            ok.append(hit)
        else:
            blocked.append((hit, reason))
    return ok, blocked


def diversify(hits: Sequence[SearchResult]) -> list[SearchResult]:
    """Best hit of each domain first (rank order kept), then the remaining hits in order.

    Cutting the list to a page limit therefore reads at most one page per domain as long as
    there are enough domains, and still fills the limit from the same domain when there are not.
    """
    first: list[SearchResult] = []
    rest: list[SearchResult] = []
    seen: set[str | None] = set()
    for hit in hits:
        domain = registrable_domain(hit.url)
        if domain is not None and domain in seen:
            rest.append(hit)
        else:
            seen.add(domain)
            first.append(hit)
    return first + rest
