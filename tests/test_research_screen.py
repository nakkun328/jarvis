"""The read-only Research screen: the page route, static files, and CSP-friendly markup."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import Settings

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
RESEARCH_FILES = [
    "research.html",
    "research.css",
    "research.js",
    "research-api.js",
    "research-view.js",
]


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(db_path=tmp_path / "screen.sqlite3"))) as test_client:
        yield test_client


class Inspector(HTMLParser):
    """Collects what a strict Content-Security-Policy would block in markup."""

    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []
        self.scripts: list[dict[str, str | None]] = []
        self.links: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
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
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.problems.append("inline script body")


def inspect(page: str) -> Inspector:
    inspector = Inspector()
    inspector.feed(page)
    return inspector


def test_research_route_serves_the_page(client: TestClient) -> None:
    response = client.get("/research")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "ローカル専用・読み取り専用" in response.text
    assert '<script type="module" src="/static/research.js">' in response.text
    assert response.text == (FRONTEND / "research.html").read_text()


def test_research_route_is_get_only_and_leaves_the_api_alone(client: TestClient) -> None:
    assert client.post("/research").status_code == 405
    assert client.get("/").status_code == 200
    assert client.get("/api/research/sessions").json() == {"sessions": []}


@pytest.mark.parametrize("name", RESEARCH_FILES)
def test_research_screen_files_are_served_from_static(client: TestClient, name: str) -> None:
    response = client.get(f"/static/{name}")
    assert response.status_code == 200
    assert response.text == (FRONTEND / name).read_text()


@pytest.mark.parametrize("name", ["research.html", "index.html"])
def test_pages_have_no_inline_script_or_style(name: str) -> None:
    inspector = inspect((FRONTEND / name).read_text())
    assert inspector.problems == []
    assert inspector.scripts
    assert all(script.get("src", "").startswith("/static/") for script in inspector.scripts)


def test_chat_and_research_pages_use_the_shared_shell() -> None:
    # The header and navigation are built by nav.js from one page list (see test_web_shell.py).
    for name in ("index.html", "research.html"):
        page = (FRONTEND / name).read_text()
        assert "data-app-header" in page
        assert '<script type="module" src="/static/shell.js">' in page
        assert "app-nav" not in page


def test_research_scripts_never_render_markup_or_write() -> None:
    forbidden = re.compile(
        r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|"
        r"createContextualFragment|\bmethod\s*:|XMLHttpRequest|sendBeacon|localStorage|"
        r"EventSource|\.setAttribute\(\s*[\"']on"
    )
    scripts = sorted(FRONTEND.glob("research*.js"))
    assert len(scripts) == 3
    for path in scripts:
        assert not forbidden.search(path.read_text()), path.name


def test_research_links_always_carry_a_safe_rel_and_a_checked_href() -> None:
    script = (FRONTEND / "research.js").read_text()
    assert script.count(".href = ") == script.count('rel = "noopener noreferrer"') == 2
    assert script.count('target = "_blank"') == 2
    # The href values come from view-model fields that safeHref produced.
    assert script.count("link.href = href;") + script.count("link.href = claim.source.href;") == 2


def test_research_screen_has_no_write_controls() -> None:
    page = (FRONTEND / "research.html").read_text()
    assert not re.search(r"<form|<input|<textarea|<select|<progress", page)
    assert not re.search(r"開始する|新規作成|削除|キャンセル", page)
