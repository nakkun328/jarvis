"""The /discussion screen (docs/discussion.md): route, shell wiring and static guarantees."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import Settings
from backend.discussion.errors import ErrorCode

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
MODULES = [
    "discussion.js",
    "discussion-api.js",
    "discussion-poll.js",
    "discussion-render.js",
    "discussion-screen.js",
    "discussion-view.js",
    "discussion-personas-api.js",
    "discussion-personas-panel.js",
    "discussion-personas-view.js",
]


def test_discussion_page_is_served_and_listed_in_the_navigation(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(db_path=tmp_path / "ui.sqlite3"))) as client:
        page = client.get("/discussion")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert page.text == (FRONTEND / "discussion.html").read_text()
        for name in ("discussion.css", "discussion-personas.css", *MODULES):
            assert client.get(f"/static/{name}").status_code == 200, name
        # With the feature off the page loads and says so; the API routes do not exist.
        assert client.get("/api/discussions/config").status_code == 404
    nav = (FRONTEND / "nav.js").read_text()
    assert '{ path: "/discussion", label: "議論" }' in nav
    assert nav.index('"/devices"') < nav.index('"/discussion"')


def test_discussion_stylesheet_is_precached_and_the_cache_version_moved() -> None:
    worker = (FRONTEND / "sw.js").read_text()
    block = worker.split("const SHELL_FILES = [", 1)[1].split("];", 1)[0]
    assert '"/static/discussion.css"' in block
    assert '"/static/discussion-personas.css"' in block
    version = re.search(r'const CACHE_VERSION = "v(\d+)";', worker)
    assert version is not None and int(version.group(1)) >= 8


def test_discussion_page_has_no_inline_script_or_style() -> None:
    html = (FRONTEND / "discussion.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "<style" not in html and " style=" not in html
    assert not re.search(r"\son[a-z]+=", html)


def test_every_discussion_error_code_has_a_message_in_the_screen() -> None:
    source = (FRONTEND / "discussion-view.js").read_text()
    for code in ErrorCode:
        assert f"{code.value}:" in source, code.value


def test_persona_editor_ids_exist_in_the_page_and_nothing_uses_innerhtml() -> None:
    panel = (FRONTEND / "discussion-personas-panel.js").read_text()
    block = panel.split("const IDS = [", 1)[1].split("];", 1)[0]
    ids = re.findall(r'"([a-z][a-z0-9-]*)"', block)
    assert len(ids) > 30
    html = (FRONTEND / "discussion.html").read_text()
    for element_id in ids:
        assert f'id="{element_id}"' in html, element_id
    assert "/static/discussion-personas.css" in html
    banned = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(")
    for name in MODULES:
        assert not banned.search((FRONTEND / name).read_text()), name


def test_persona_route_error_codes_have_screen_messages() -> None:
    source = (FRONTEND / "discussion-view.js").read_text()
    for code in ErrorCode:
        if code.value.startswith("PERSONA_"):
            assert f"{code.value}:" in source, code.value
