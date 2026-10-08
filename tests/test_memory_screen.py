"""The read-only Memory screen: the page route, static files, and its safety contract."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import Settings

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
MEMORY_FILES = [
    "memory.html",
    "memory.css",
    "memory.js",
    "memory-api.js",
    "memory-view.js",
]


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(db_path=tmp_path / "screen.sqlite3"))) as test_client:
        yield test_client


def test_memory_route_serves_the_page(client: TestClient) -> None:
    response = client.get("/memory")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<script type="module" src="/static/memory.js">' in response.text
    assert response.text == (FRONTEND / "memory.html").read_text()


def test_memory_route_is_get_only_and_leaves_other_routes_alone(client: TestClient) -> None:
    assert client.post("/memory").status_code == 405
    assert client.get("/").status_code == 200
    assert client.get("/api/memory/notes").json() == {"notes": []}


@pytest.mark.parametrize("name", MEMORY_FILES)
def test_memory_screen_files_are_served_from_static(client: TestClient, name: str) -> None:
    response = client.get(f"/static/{name}")
    assert response.status_code == 200
    assert response.text == (FRONTEND / name).read_text()


def test_banner_says_read_only_and_only_reviewed_notes_reach_the_llm() -> None:
    page = (FRONTEND / "memory.html").read_text()
    assert "ローカル専用・読み取り専用" in page
    assert "LLM に渡されるのは、レビュー済みで現在も承認されているノートだけ" in page
    assert "承認時点の記録" in page


def test_memory_scripts_never_render_markup_or_write() -> None:
    forbidden = re.compile(
        r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|"
        r"createContextualFragment|\bmethod\s*:|XMLHttpRequest|sendBeacon|localStorage|"
        r"EventSource|\.setAttribute\(\s*[\"']on"
    )
    scripts = sorted(FRONTEND.glob("memory*.js"))
    assert [path.name for path in scripts] == ["memory-api.js", "memory-view.js", "memory.js"]
    for path in scripts:
        assert not forbidden.search(path.read_text()), path.name


def test_memory_screen_has_no_write_controls() -> None:
    page = (FRONTEND / "memory.html").read_text()
    assert not re.search(r"<form|<input|<textarea|<select|<progress", page)
    assert not re.search(r"承認する|却下する|編集する|削除する|訂正する|撤回する", page)
    # Only the read-only GET endpoints are ever used.
    api = (FRONTEND / "memory-api.js").read_text()
    assert sorted(set(re.findall(r"/api/memory/[a-z]+", api))) == [
        "/api/memory/candidates",
        "/api/memory/notes",
    ]
    assert not re.search(r"\b(POST|PUT|PATCH|DELETE)\b", api)


def test_memory_screen_never_links_to_vault_files() -> None:
    for name in MEMORY_FILES:
        text = (FRONTEND / name).read_text()
        assert not re.search(r"obsidian://|file://|\.md\b", text), name
