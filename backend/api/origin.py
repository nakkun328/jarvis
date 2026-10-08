"""Same-origin guard for browser requests that change state.

A web page on another site can make the visitor's browser send a POST to ``localhost``. The
guard refuses such a request when the browser says where it came from: an ``Origin`` (else
``Referer``) that does not name the host the request was addressed to, or a ``Sec-Fetch-Site``
other than same-origin/none. A request that carries none of these headers is not a browser
form or fetch (browsers always send ``Origin`` on a cross-site POST), so it is let through;
the login layer, when enabled, is stricter and requires the header.
"""

from starlette.types import Scope

from backend.auth.middleware import is_same_origin

_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


def _values(scope: Scope, name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in scope.get("headers", ()) if key == name]


def is_cross_origin_request(scope: Scope, trusted_proxy: bool = False) -> bool:
    """True when the request must be refused as coming from another origin."""
    fetch_site = _values(scope, b"sec-fetch-site")
    if fetch_site and (len(fetch_site) != 1 or fetch_site[0].lower() not in _ALLOWED_FETCH_SITES):
        return True
    if _values(scope, b"origin") or _values(scope, b"referer"):
        return not is_same_origin(scope, trusted_proxy)
    return False
