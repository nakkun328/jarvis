"""The owner's trial script, exercised without a key and without any network."""

import json
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from backend.research.tavily import TavilySearchProvider
from scripts import search_trial as cli

CREDENTIAL = "tvly-test-credential-0123456789abcdef"
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def forbid_real_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a test attempted a real network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def use_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        cli,
        "TavilySearchProvider",
        lambda credential: TavilySearchProvider(
            credential, transport=httpx.MockTransport(recording)
        ),
    )
    return seen


def test_dry_run_needs_no_key_and_prints_only_the_allowed_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--dry-run", "one", "two"], environ={}) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "[2/2]" in out
    assert "Example result one" in out and "https://example.com/one" in out
    assert "snippet: 43 chars, published: 2026-01-02" in out
    assert "credits used: 1" in out and "Done: 2 queries, 2 credits" in out
    for forbidden in ("score", "Authorization", "Bearer", "search_depth", "usage", "request_id"):
        assert forbidden not in out


def test_dry_run_uses_the_built_in_queries_by_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--dry-run"], environ={}) == 0
    assert "[3/3]" in capsys.readouterr().out
    assert len(cli.DEFAULT_QUERIES) == 3


def test_refuses_more_than_ten_queries(capsys: pytest.CaptureFixture[str]) -> None:
    queries = [f"q{n}" for n in range(11)]
    assert cli.main(["--dry-run", *queries], environ={}) == 2
    assert "more than 10" in capsys.readouterr().err
    assert cli.main(["--dry-run", *queries[:10]], environ={}) == 0


@pytest.mark.parametrize(
    "argv", [["--max-results", "0"], ["--max-results", "11"], ["  "], ["x" * 501]]
)
def test_rejects_bad_arguments_before_any_call(argv: list[str]) -> None:
    assert cli.main(["--dry-run", *argv], environ={}) == 2


def test_real_mode_requires_the_key_from_the_environment(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = use_transport(monkeypatch, lambda request: httpx.Response(200, json={"results": []}))
    assert cli.main(["q"], environ={}) == 2
    assert cli.main(["q"], environ={"JARVIS_SEARCH_API_KEY": "  "}) == 2
    assert cli.main(["q"], environ={"JARVIS_SEARCH_API_KEY": "bad key"}) == 2
    err = capsys.readouterr().err
    assert "JARVIS_SEARCH_API_KEY" in err and "bad key" not in err
    assert seen == []


def test_key_is_not_accepted_on_the_command_line() -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["--api-key", CREDENTIAL, "--dry-run"], environ={})
    assert info.value.code == 2


def test_real_mode_prints_results_but_never_the_key_or_body(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answer": "VENDOR ANSWER",
                "results": [
                    {
                        "title": "Hello",
                        "url": "https://example.com/h",
                        "content": "abc",
                        "raw_content": "PAGE TEXT",
                        "score": 0.3,
                    }
                ],
                "usage": {"credits": 1},
            },
        )

    seen = use_transport(monkeypatch, handler)
    assert cli.main(["private query words"], environ={"JARVIS_SEARCH_API_KEY": CREDENTIAL}) == 0
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "Hello" in text and "https://example.com/h" in text and "snippet: 3 chars" in text
    assert "published: -" in text and "credits used: 1" in text
    secrets_and_bodies = (CREDENTIAL, "VENDOR ANSWER", "PAGE TEXT", "private query words")
    for forbidden in (*secrets_and_bodies, "include_answer"):
        assert forbidden not in text
    assert json.loads(seen[0].content)["search_depth"] == "basic"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "unauthorized"),
        (429, "rate_limited"),
        (432, "quota_exhausted"),
        (400, "invalid_query"),
    ],
)
def test_any_error_stops_with_a_fixed_message_and_nonzero_exit(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, status: int, code: str
) -> None:
    seen = use_transport(
        monkeypatch, lambda request: httpx.Response(status, text=f"vendor says {CREDENTIAL}")
    )
    exit_code = cli.main(["a", "b", "c"], environ={"JARVIS_SEARCH_API_KEY": CREDENTIAL})
    captured = capsys.readouterr()
    assert exit_code == 1
    assert f"Search failed ({code}). Stopped." in captured.err
    assert CREDENTIAL not in captured.out + captured.err and "vendor says" not in captured.err
    assert len(seen) == 1  # no further queries after the first error


def test_script_runs_as_a_subprocess_in_dry_run() -> None:
    environment = {"PATH": "", "JARVIS_SEARCH_API_KEY": ""}
    done = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "search_trial.py"), "--dry-run", "q"],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "Example result one" in done.stdout
