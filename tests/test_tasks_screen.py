"""The read-only Tasks screen: the page route, static files, and CSP-friendly markup."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import Settings

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
TASK_FILES = [
    "tasks.html",
    "tasks.css",
    "tasks.js",
    "tasks-api.js",
    "tasks-stream.js",
    "tasks-view.js",
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


def test_tasks_route_serves_the_page(client: TestClient) -> None:
    response = client.get("/tasks")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "ローカル専用・読み取り専用" in response.text
    assert '<script type="module" src="/static/tasks.js">' in response.text
    assert response.text == (FRONTEND / "tasks.html").read_text()


def test_tasks_route_is_get_only_and_leaves_the_api_alone(client: TestClient) -> None:
    assert client.post("/tasks").status_code == 405
    assert client.get("/").status_code == 200
    assert client.get("/api/tasks").json() == {"tasks": []}


@pytest.mark.parametrize("name", TASK_FILES)
def test_task_screen_files_are_served_from_static(client: TestClient, name: str) -> None:
    response = client.get(f"/static/{name}")
    assert response.status_code == 200
    assert response.text == (FRONTEND / name).read_text()


@pytest.mark.parametrize("name", ["tasks.html", "index.html"])
def test_pages_have_no_inline_script_or_style(name: str) -> None:
    inspector = inspect((FRONTEND / name).read_text())
    assert inspector.problems == []
    assert inspector.scripts
    assert all(script.get("src", "").startswith("/static/") for script in inspector.scripts)


def test_chat_and_tasks_pages_link_to_each_other() -> None:
    assert "/tasks" in inspect((FRONTEND / "index.html").read_text()).links
    assert "/" in inspect((FRONTEND / "tasks.html").read_text()).links


def test_task_screen_scripts_never_render_markup_or_write() -> None:
    forbidden = re.compile(
        r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|"
        r"createContextualFragment|\bmethod\s*:|XMLHttpRequest|sendBeacon|localStorage"
    )
    for path in FRONTEND.glob("tasks*.js"):
        assert not forbidden.search(path.read_text()), path.name


def test_task_screen_has_no_write_controls_or_percentages() -> None:
    page = (FRONTEND / "tasks.html").read_text()
    assert not re.search(r"<form|<input|<textarea|<select|<progress|role=\"progressbar\"", page)
    assert not re.search(r"キャンセル|再試行する|新規作成|削除", page)
    for name in ("tasks-view.js", "tasks.js"):
        assert not re.search(r"toFixed|Math\.round\(.*\*\s*100|%\)", (FRONTEND / name).read_text())
