"""The read-only Devices screen and API: truthful facts only, no registry."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.devices import summarize_user_agent
from backend.core.config import Settings

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
EDGE_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
)


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "devices.sqlite3")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (FIREFOX_LINUX, {"browser": "Firefox", "os": "Linux"}),
        (SAFARI_IPHONE, {"browser": "Safari", "os": "iOS"}),
        (EDGE_WINDOWS, {"browser": "Edge", "os": "Windows"}),
        ("curl/8.0", {"browser": "unknown", "os": "unknown"}),
        (None, {"browser": "unknown", "os": "unknown"}),
    ],
)
def test_user_agent_is_summarised_to_families(agent: str | None, expected: dict[str, str]) -> None:
    assert summarize_user_agent(agent) == expected


def test_devices_answers_with_current_server_and_empty_registry(client: TestClient) -> None:
    response = client.get("/api/devices", headers={"User-Agent": FIREFOX_LINUX})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["current"]["browser"] == "Firefox"
    assert body["current"]["os"] == "Linux"
    assert body["current"]["connection"] == "remote"  # the test client is not a loopback peer
    assert body["current"]["scheme"] == "http"
    assert body["server"]["login_enabled"] is False
    assert body["server"]["providers"] == {"chat": "none", "search": "none"}
    assert body["registered_devices"] == {
        "available": False,
        "reason": "no_device_registry",
        "devices": [],
    }


def test_response_never_echoes_the_raw_user_agent_or_an_address(client: TestClient) -> None:
    text = client.get("/api/devices", headers={"User-Agent": FIREFOX_LINUX}).text
    assert "Gecko" not in text and "rv:130" not in text
    assert not re.search(r"\d+\.\d+\.\d+\.\d+|testclient", text)


def test_devices_is_get_only(client: TestClient) -> None:
    assert client.post("/api/devices").status_code == 405
    assert client.delete("/api/devices").status_code == 405


def test_trusted_proxy_connection_is_not_claimed_to_be_local(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "p.sqlite3", trusted_proxy=True)
    with TestClient(create_app(settings)) as proxied:
        assert proxied.get("/api/devices").json()["current"]["connection"] == "proxied"


def test_devices_is_not_a_public_path() -> None:
    from backend.auth.middleware import is_public

    assert not is_public("GET", "/api/devices")
    assert not is_public("GET", "/devices")
    assert not is_public("GET", "/static/devices.js")


def test_devices_page_route_and_files(client: TestClient) -> None:
    page = client.get("/devices")
    assert page.status_code == 200
    assert page.text == (FRONTEND / "devices.html").read_text()
    assert "data-app-header" in page.text
    assert not re.search(r"<script(?![^>]*src=)|\son\w+=|\sstyle=|<form|<input", page.text)
    for name in ("devices.css", "devices.js", "devices-api.js", "devices-view.js"):
        assert client.get(f"/static/{name}").text == (FRONTEND / name).read_text()
    assert "/static/devices.css" in (FRONTEND / "sw.js").read_text()
    assert client.post("/devices").status_code == 405
