"""The chat page's Activity View: static files, CSP-friendly markup and the safety rules."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import Settings

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
ACTIVITY_FILES = ["activity.js", "activity-view.js", "activity.css"]


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(db_path=tmp_path / "screen.sqlite3"))) as test_client:
        yield test_client


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []
        self.stylesheets: list[str] = []
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, _value in attrs:
            if name.startswith("on") or name == "style":
                self.problems.append(f"{tag} {name}")
        attributes = dict(attrs)
        if tag == "style":
            self.problems.append("inline style element")
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.stylesheets.append(attributes.get("href") or "")
        if attributes.get("id"):
            self.ids.append(attributes["id"] or "")


def page() -> Page:
    parsed = Page()
    parsed.feed((FRONTEND / "index.html").read_text())
    return parsed


@pytest.mark.parametrize("name", ACTIVITY_FILES)
def test_activity_files_are_served_from_static(client: TestClient, name: str) -> None:
    response = client.get(f"/static/{name}")
    assert response.status_code == 200
    assert response.text == (FRONTEND / name).read_text()


def test_chat_page_has_the_mount_and_loads_the_stylesheet() -> None:
    parsed = page()
    assert parsed.problems == []
    assert "activity" in parsed.ids
    assert "/static/activity.css" in parsed.stylesheets
    # The panel is built by activity.js; the page carries only the empty mount.
    assert 'class="activity-mount" id="activity"></div>' in (FRONTEND / "index.html").read_text()


def test_activity_scripts_never_render_markup_or_use_storage_or_the_network() -> None:
    forbidden = re.compile(
        r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|"
        r"createContextualFragment|fetch\(|XMLHttpRequest|sendBeacon|localStorage|sessionStorage|"
        r"\.style\b|setAttribute\(\s*[\"']style"
    )
    for name in ("activity.js", "activity-view.js"):
        assert not forbidden.search((FRONTEND / name).read_text()), name


def test_activity_view_only_listens_to_the_activity_event() -> None:
    # The pure module takes parsed activity payloads and never reads message or reply text.
    source = (FRONTEND / "activity-view.js").read_text()
    assert not re.search(r"\bdelta\b|\.reply\b|\.message\b|conversation_id", source)
    assert 'event === "activity"' in (FRONTEND / "chat-api.js").read_text()


def test_stylesheet_has_no_remote_assets_and_honours_reduced_motion() -> None:
    css = (FRONTEND / "activity.css").read_text()
    assert "@import" not in css and "url(" not in css and "http" not in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert 'data-reduced-motion="true"' in css and ".reduce-motion" in css
    assert "@media (max-width: 600px)" in css


def test_every_orb_mode_the_view_can_produce_has_a_style() -> None:
    source = (FRONTEND / "activity-view.js").read_text()
    block = source.split("function orbMode", 1)[1].split("function describe", 1)[0]
    modes = set(re.findall(r'return "([a-z]+)"', block))
    expected = {"idle", "listen", "recall", "research", "synth", "speak", "done", "error", "stopped"}
    assert modes >= expected
    css = (FRONTEND / "activity.css").read_text()
    for mode in modes:
        assert f'data-mode="{mode}"' in css, mode


def test_activity_stylesheet_is_precached_by_the_service_worker() -> None:
    assert '"/static/activity.css"' in (FRONTEND / "sw.js").read_text()


def test_reserved_stages_are_documented_as_not_emitted() -> None:
    text = (Path(__file__).resolve().parents[1] / "docs" / "chat.md").read_text()
    assert "## Activity events" in text
    for stage in ("routing", "route_selected", "researching", "speaking"):
        assert stage in text.split("## Activity events", 1)[1]
