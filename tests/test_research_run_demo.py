"""The dev demo server's fakes: each scenario ends the way its marker promises (no uvicorn)."""

import importlib.util
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import Settings
from backend.research.reader import PageReader

pytestmark = pytest.mark.usefixtures("no_network")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev_research_run_demo_server.py"


@pytest.fixture(scope="module")
def demo():
    spec = importlib.util.spec_from_file_location("dev_research_run_demo_server", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(demo, tmp_path: Path):
    search, transport, resolver, model = demo.build_fakes(True, tmp_path / "demo.sqlite3")
    app = create_app(
        Settings(db_path=tmp_path / "demo.sqlite3", research_enabled=True),
        model,
        search_provider=search,
        page_reader=PageReader(transport, resolver),
    )
    with TestClient(app) as test_client:
        yield test_client


def run(client: TestClient, question: str, level: str) -> dict:
    response = client.post("/api/research/sessions", json={"question": question, "level": level})
    assert response.status_code == 202
    session_id = response.json()["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        detail = client.get(f"/api/research/sessions/{session_id}").json()
        if detail["status"] in ("completed", "failed", "cancelled"):
            return detail
        time.sleep(0.02)
    raise AssertionError("demo run did not finish")


def test_the_port_is_reserved_nowhere_else(demo) -> None:
    assert demo.DEFAULT_PORT == 18971
    assert demo.DEFAULT_PORT not in demo.RESERVED_PORTS


@pytest.mark.parametrize("level", ["quick", "standard"])
def test_default_scenario_gives_a_cited_result(client: TestClient, level: str) -> None:
    detail = run(client, "How long does the Foo widget cache keep entries?", level)
    assert detail["status"] == "completed"
    assert len(detail["claims"]) >= 2 and detail["sources"]


def test_fail_scenario_fails_with_a_fixed_reason(client: TestClient) -> None:
    detail = run(client, "How long does the Foo widget cache keep entries? #fail", "quick")
    assert (detail["status"], detail["failure_reason"]) == ("failed", "reader_failed")


def test_hostile_scenario_keeps_markup_as_plain_strings(client: TestClient) -> None:
    detail = run(client, "How long does the Foo widget cache keep entries? #hostile", "standard")
    assert detail["status"] == "completed"
    assert any("<script>" in claim["quote"] for claim in detail["claims"])


def test_conflict_scenario_lists_an_open_conflict(client: TestClient) -> None:
    detail = run(client, "How long does the Foo widget cache keep entries? #conflict", "standard")
    assert detail["status"] == "completed"
    assert any(conflict["status"] == "open" for conflict in detail["conflicts"])
    assert "Open conflicts" in detail["result_text"]


def test_nothing_scenario_completes_with_the_no_claim_notice(client: TestClient) -> None:
    detail = run(client, "How long does the Foo widget cache keep entries? #nothing", "quick")
    assert detail["status"] == "completed" and detail["claims"] == []
    assert "No claim could be verified" in detail["result_text"]
