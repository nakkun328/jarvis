"""The shared Web shell (header, navigation, tokens) and the conservative PWA files."""

import json
import re
import struct
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
PAGE_FILES = ["index.html", "tasks.html", "approvals.html", "research.html", "memory.html"]


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(db_path=tmp_path / "shell.sqlite3"))) as test_client:
        yield test_client


class Inspector(HTMLParser):
    """Collects what a strict Content-Security-Policy would block, and the page's structure."""

    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []
        self.scripts: list[dict[str, str | None]] = []
        self.stylesheets: list[str] = []
        self.rels: dict[str, str] = {}
        self.tags: list[str] = []
        self.classes: set[str] = set()
        self.attrs: set[str] = set()
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.tags.append(tag)
        self.attrs.update(attributes)
        self.classes.update((attributes.get("class") or "").split())
        for name, value in attrs:
            if name.startswith("on"):
                self.problems.append(f"inline handler {name}")
            if name == "style":
                self.problems.append("inline style attribute")
            if name in {"href", "src"} and (value or "").strip().lower().startswith("javascript:"):
                self.problems.append("javascript: URL")
        if tag == "script":
            self.scripts.append(attributes)
            self._in_script = True
        if tag == "style":
            self.problems.append("inline style element")
        if tag == "link":
            self.rels[attributes.get("rel") or ""] = attributes.get("href") or ""
            if attributes.get("rel") == "stylesheet":
                self.stylesheets.append(attributes.get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.problems.append("inline script body")


def inspect(name: str) -> Inspector:
    inspector = Inspector()
    inspector.feed((FRONTEND / name).read_text())
    return inspector


def declared_pages() -> list[tuple[str, str]]:
    source = (FRONTEND / "nav.js").read_text()
    return re.findall(r'\{ path: "([^"]+)", label: "([^"]+)" \}', source)


def shell_files() -> list[str]:
    source = (FRONTEND / "sw.js").read_text()
    block = source.split("const SHELL_FILES = [", 1)[1].split("];", 1)[0]
    return re.findall(r'"([^"]+)"', block)


# ----- shared layout -----


def test_every_declared_page_is_a_served_html_route(client: TestClient) -> None:
    pages = declared_pages()
    assert [path for path, _ in pages] == ["/", "/tasks", "/approvals", "/research", "/memory"]
    for path, _label in pages:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("name", PAGE_FILES)
def test_pages_share_one_header_and_load_the_shell_once(name: str) -> None:
    inspector = inspect(name)
    assert inspector.problems == []
    assert "data-app-header" in inspector.attrs
    # The navigation is built by nav.js; no page carries its own copy of it.
    assert "nav" not in inspector.tags
    assert not inspector.classes & {"app-nav", "nav-link", "brand", "brand-mark"}
    srcs = [script.get("src") for script in inspector.scripts]
    assert srcs.count("/static/shell.js") == 1
    assert all(script.get("type") == "module" for script in inspector.scripts)
    assert inspector.stylesheets[0] == "/static/shell.css"
    assert inspector.tags.count("main") == 1
    assert inspector.rels["manifest"] == "/manifest.webmanifest"
    assert inspector.tags.count("header") == 1


def test_every_declared_page_has_an_html_file_that_loads_the_shell() -> None:
    labels = dict(declared_pages())
    assert len(labels) == len(PAGE_FILES)
    for name in PAGE_FILES:
        title = re.search(r"<title>(.*?)</title>", (FRONTEND / name).read_text()).group(1)
        assert title.startswith("JARVIS")


def test_nav_and_shell_scripts_never_render_markup() -> None:
    forbidden = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(")
    for name in ("nav.js", "shell.js", "pwa.js", "sw.js", "session.js"):
        assert not forbidden.search((FRONTEND / name).read_text()), name


@pytest.mark.parametrize("name", ["shell.css", "shell.js", "nav.js", "pwa.js", "session.js"])
def test_shell_files_are_served_from_static(client: TestClient, name: str) -> None:
    response = client.get(f"/static/{name}")
    assert response.status_code == 200
    assert response.text == (FRONTEND / name).read_text()


def test_shared_components_are_defined_once() -> None:
    shared = (FRONTEND / "shell.css").read_text()
    for selector in (".readonly-note {", ".badge {", ".filters {", ".app-header {", ".nav-link {"):
        assert selector in shared
        for name in ("style.css", "tasks.css", "approvals.css", "research.css", "memory.css"):
            assert selector not in (FRONTEND / name).read_text(), (selector, name)


def test_no_stylesheet_uses_inline_importing_or_remote_assets() -> None:
    for path in FRONTEND.glob("*.css"):
        text = path.read_text()
        assert "@import" not in text and "http://" not in text and "https://" not in text, path.name


# ----- PWA -----


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_manifest_is_served_with_its_media_type(client: TestClient) -> None:
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["name"] == "JARVIS"
    assert manifest["start_url"] == "/" and manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["theme_color"] == manifest["background_color"] == "#0b1420"


def test_manifest_icons_exist_and_have_the_declared_size(client: TestClient) -> None:
    manifest = client.get("/manifest.webmanifest").json()
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert {icon["purpose"] for icon in manifest["icons"]} >= {"any", "maskable"}
    for icon in manifest["icons"]:
        assert icon["src"].startswith("/static/icons/")
        assert client.get(icon["src"]).status_code == 200
        if icon["type"] == "image/png":
            width, height = icon["sizes"].split("x")
            assert png_size(ROOT / "frontend" / icon["src"].removeprefix("/static/")) == (
                int(width), int(height),
            )


def test_manifest_has_no_external_url() -> None:
    text = (FRONTEND / "manifest.webmanifest").read_text()
    assert "http://" not in text and "https://" not in text
    json.loads(text)


def test_service_worker_is_served_from_the_root_with_scope_and_no_cache(
    client: TestClient,
) -> None:
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["service-worker-allowed"] == "/"
    assert response.headers["cache-control"] == "no-cache"
    assert response.text == (FRONTEND / "sw.js").read_text()


@pytest.mark.parametrize("path", ["/sw.js", "/manifest.webmanifest", "/memory"])
def test_pwa_and_memory_routes_are_get_only(client: TestClient, path: str) -> None:
    assert client.post(path).status_code == 405


def test_service_worker_precaches_only_static_shell_assets() -> None:
    files = shell_files()
    assert files
    assert len(files) == len(set(files))
    for path in files:
        assert re.fullmatch(r"/static/[A-Za-z0-9_./-]+\.(css|js|png|svg)", path), path
        assert (FRONTEND / path.removeprefix("/static/")).is_file(), path
    assert not [path for path in files if path.endswith(".html") or "/api" in path]


def test_every_stylesheet_and_shell_script_the_pages_load_is_precached() -> None:
    cached = set(shell_files())
    for name in PAGE_FILES:
        inspector = inspect(name)
        assert set(inspector.stylesheets) <= cached, name
    assert {"/static/shell.js", "/static/nav.js", "/static/pwa.js"} <= cached


def test_service_worker_version_and_cleanup_are_declared() -> None:
    source = (FRONTEND / "sw.js").read_text()
    assert re.search(r'const CACHE_VERSION = "v\d+";', source)
    assert 'const CACHE_PREFIX = "jarvis-shell-";' in source
    assert "caches.delete" in source and "name !== CACHE_NAME" in source


def test_offline_and_login_caching_are_documented() -> None:
    text = (ROOT / "docs" / "web-shell.md").read_text()
    assert "offline use is not supported" in text.lower()
    assert "login" in text.lower() and "must not be cached" in text.lower()


# ----- login session (logout button, 401 redirect) -----


def test_pwa_and_session_files_are_listed_as_shell_files() -> None:
    cached = set(shell_files())
    assert {"/static/session.js", "/static/shell.js"} <= cached
    # The login page is not part of the shell and is never cached.
    assert not [path for path in cached if "login" in path]


def test_only_the_shell_session_module_and_the_login_page_touch_the_auth_api() -> None:
    users = sorted(
        path.name for path in FRONTEND.glob("*.js") if "/api/auth/" in path.read_text()
    )
    assert users == ["login.js", "session.js"]
    session = (FRONTEND / "session.js").read_text()
    assert sorted(set(re.findall(r"/api/auth/[a-z]+", session))) == [
        "/api/auth/logout",
        "/api/auth/status",
    ]
    assert session.count('method: "POST"') == 1
    for sink in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        assert sink not in session


def test_every_background_fetch_helper_handles_a_refused_session() -> None:
    helpers = (
        "chat-api.js",
        "tasks-api.js",
        "approvals-api.js",
        "research-api.js",
        "research-run-api.js",
        "memory-api.js",
        "tasks-stream.js",
    )
    for name in helpers:
        source = (FRONTEND / name).read_text()
        assert 'from "./session.js"' in source, name
        assert "sessionEnded()" in source, name
        assert "401" in source, name


def test_the_login_page_does_not_load_the_shell_or_the_session_module() -> None:
    page = (FRONTEND / "login.html").read_text()
    assert "shell.js" not in page and "session.js" not in page and "manifest" not in page
    assert "session.js" not in (FRONTEND / "login.js").read_text()


def test_the_logout_button_is_not_in_any_page_markup() -> None:
    # It is added by session.js only when login is on and this browser is signed in.
    for name in PAGE_FILES:
        assert "logout" not in (FRONTEND / name).read_text().lower(), name


def test_pages_without_login_show_no_logout_and_have_no_auth_routes(client: TestClient) -> None:
    assert client.get("/api/auth/status").status_code == 404
    assert client.post("/api/auth/logout").status_code == 404


def test_docs_explain_the_pwa_and_auth_decision() -> None:
    web = (ROOT / "docs" / "web-shell.md").read_text()
    auth = (ROOT / "docs" / "auth.md").read_text()
    for text in (web, auth):
        assert "/manifest.webmanifest" in text and "exact" in text.lower()
        assert "/static/icons/icon-192.png" in text
        assert "use-credentials" in text
    assert "/sw.js" in auth and "logout" in auth.lower()
