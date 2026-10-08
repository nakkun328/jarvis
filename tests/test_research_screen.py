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
    "research-run.js",
    "research-run-api.js",
    "research-run-view.js",
]
NOTICE = (
    "調べるときは、質問文が検索サービス(Tavily)へ送信されます。"
    "サービス側で保持・利用される可能性があります。記憶の内容は送信しません。"
)


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
    assert "ローカル専用" in response.text
    assert "読み取り専用" not in response.text
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


def test_research_scripts_never_render_markup_or_use_other_channels() -> None:
    forbidden = re.compile(
        r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|"
        r"createContextualFragment|XMLHttpRequest|sendBeacon|localStorage|sessionStorage|"
        r"EventSource|WebSocket|\.setAttribute\(\s*[\"']on"
    )
    scripts = sorted(FRONTEND.glob("research*.js"))
    assert len(scripts) == 6
    for path in scripts:
        assert not forbidden.search(path.read_text()), path.name


def test_only_the_run_client_writes_and_only_with_two_posts() -> None:
    for path in sorted(FRONTEND.glob("research*.js")):
        text = path.read_text()
        methods = re.findall(r"\bmethod\s*:\s*[\"'](\w+)[\"']", text)
        if path.name == "research-run-api.js":
            assert methods == ["POST", "POST"]
            assert not re.search(r"PUT|PATCH|DELETE", text)
        else:
            assert methods == [], path.name


def test_research_links_always_carry_a_safe_rel_and_a_checked_href() -> None:
    script = (FRONTEND / "research.js").read_text()
    assert script.count(".href = ") == script.count('rel = "noopener noreferrer"') == 2
    assert script.count('target = "_blank"') == 2
    # The href values come from view-model fields that safeHref produced.
    assert script.count("link.href = href;") + script.count("link.href = claim.source.href;") == 2
    run = (FRONTEND / "research-run.js").read_text()
    assert run.count(".href = ") == run.count('rel = "noopener noreferrer"') == 1
    assert run.count('target = "_blank"') == 1
    assert run.count("link.href = source.href;") == 1
    assert "if (source.href)" in run


def test_the_run_form_has_labels_a_counter_and_the_notice_beside_the_button() -> None:
    page = (FRONTEND / "research.html").read_text()
    assert '<label class="run-label" for="run-question">' in page
    assert '<label class="run-label" for="run-level">' in page
    assert 'id="run-counter"' in page
    for needed in ("<form", "<textarea", "<select", 'type="submit"'):
        assert needed in page
    assert NOTICE in page
    # The notice sits in the form directly before the button and describes it.
    assert page.index(NOTICE) < page.index('id="run-submit"')
    assert page.index('id="run-notice"') < page.index('id="run-submit"')
    assert 'aria-describedby="run-notice"' in page
    assert 'role="alert"' in page and 'aria-live="polite"' in page
    assert "novalidate" in page and "maxlength" not in page  # a long text is flagged, not cut


def test_the_notice_text_is_the_same_in_the_page_and_the_view_module() -> None:
    view = (FRONTEND / "research-run-view.js").read_text()
    assert NOTICE in view
    assert view.count("Tavily") == 1


def test_the_result_texts_the_client_translates_cover_every_server_caveat() -> None:
    from backend.research.runner import QUICK_NO_CLAIM_NOTICE
    from backend.research.standard import CAVEAT_TEXT, Caveat

    view = (FRONTEND / "research-run-view.js").read_text()
    for caveat, sentence in CAVEAT_TEXT.items():
        if caveat in (Caveat.READS_FAILED, Caveat.CLAIMS_REMOVED):
            continue  # shown with a count in front; the client has a pattern for the pair
        assert f'"{sentence}"' in view, caveat
    assert "Some pages could not be read." in view
    assert "Some proposed claims were removed because they could not be verified." in view
    assert QUICK_NO_CLAIM_NOTICE in view


def test_failure_labels_cover_every_failure_reason_the_server_can_store() -> None:
    from backend.research.models import FailureReason

    view = (FRONTEND / "research-view.js").read_text()
    for reason in FailureReason:
        assert f"{reason.value}:" in view, reason


def test_every_error_code_the_api_sends_has_a_client_message() -> None:
    import backend.api.research as api

    messages = (FRONTEND / "research-run-view.js").read_text()
    codes = [
        value
        for name, value in vars(api).items()
        if name.startswith("ERROR_") and isinstance(value, str)
    ]
    assert len(codes) >= 12
    for code in [*codes, "busy", "search_budget_exhausted", "not_cancellable"]:
        if code in {"invalid_limit", "invalid_status"}:
            continue  # list filters; the screen never sends them
        assert f"  {code}:" in messages, code


def test_research_screen_scripts_are_modules_loaded_from_static() -> None:
    page = (FRONTEND / "research.html").read_text()
    assert '<script type="module" src="/static/research.js">' in page
    assert "research-run" not in page  # imported by research.js, not by the page
