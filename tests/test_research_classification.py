"""Source type classification (JAR-42): URL and title only, nothing fetched."""

import json
import socket
from pathlib import Path

import pytest

from backend.research.classification import (
    RULES,
    Basis,
    Rule,
    classify_source,
    classify_source_detailed,
)
from backend.research.models import SourceType

FIXTURE = Path(__file__).parent / "fixtures" / "research-r2-v1" / "classification.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["url"][:50] or "(empty)" for c in CASES])
def test_rule_table(case: dict) -> None:
    result = classify_source_detailed(case["url"], case.get("title"))
    assert result.source_type == SourceType(case["type"])
    assert result.rule_id == case["rule"]
    assert classify_source(case["url"], case.get("title")) == result.source_type


def test_every_source_type_but_unknown_is_reachable() -> None:
    reached = {classify_source(case["url"], case.get("title")) for case in CASES}
    assert reached == set(SourceType)


def test_rule_ids_are_unique_and_documented_basis_is_reported() -> None:
    ids = [rule.rule_id for rule in RULES]
    assert len(ids) == len(set(ids))
    assert classify_source_detailed("https://arxiv.org/abs/1").basis is Basis.HOST
    assert classify_source_detailed("https://example.com/docs/x").basis is Basis.PATH
    assert (
        classify_source_detailed("https://example.com/", "Reference Documentation").basis
        is Basis.TITLE
    )
    assert classify_source_detailed("https://example.com/").basis is Basis.DEFAULT


def test_host_beats_path_and_title() -> None:
    assert classify_source("https://arxiv.org/blog/docs/", "Documentation") is SourceType.ACADEMIC
    assert classify_source("https://www.reddit.com/docs/", "Documentation") is SourceType.COMMUNITY


def test_title_is_only_consulted_when_the_url_matches_nothing() -> None:
    assert classify_source("https://example.com/x", "Some Documentation") is SourceType.DOCS
    assert classify_source("https://example.com/x", None) is SourceType.UNKNOWN
    assert classify_source("https://example.com/x", "") is SourceType.UNKNOWN
    assert classify_source("https://example.com/x", "リファレンス") is SourceType.DOCS
    # a title cannot turn an unknown site into an official or academic one
    assert (
        classify_source("https://example.com/x", "Official Government Journal")
        is SourceType.UNKNOWN
    )


@pytest.mark.parametrize(
    "url",
    [
        "HTTPS://DOCS.PYTHON.ORG/3/",
        "https://docs.python.org./3/",
        "https://docs.python.org:443/3/",
        "https://user@docs.python.org/3/",
    ],
)
def test_case_trailing_dot_port_and_userinfo_do_not_change_the_host_rule(url: str) -> None:
    assert classify_source(url) is SourceType.DOCS


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.python.org.evil.example/",
        "https://evil.example/docs.python.org/",
        "https://arxiv.org.evil.example/",
        "https://xn--arxiv-9ra.org/abs/1",
        "https://gov.example.com/",
        "https://example.gov.evil.example/",
        "https://reddit.com.evil.example/",
        "https://evilreddit.com/",
    ],
)
def test_lookalike_hosts_do_not_match_host_rules(url: str) -> None:
    # Name patterns (docs., forum., blog.) are documented as spoofable and may still match;
    # the host tables (academic, official, community) must not.
    kind = classify_source(url)
    assert kind not in {SourceType.ACADEMIC, SourceType.OFFICIAL, SourceType.COMMUNITY}


@pytest.mark.parametrize(
    "url",
    [
        None,
        42,
        b"https://docs.python.org/",
        "https://" + "a" * 3000 + ".com/",
        "https://docs.python.org/\x00",
        "https://docs.python.org/ x",
        "https://docs.python.org/\n",
        "file:///etc/passwd",
        "data:text/html,<h1>x</h1>",
        "//docs.python.org/",
        "https:///docs/",
        "https://[::1",
    ],
)
def test_invalid_input_is_unknown_and_never_raises(url: object) -> None:
    result = classify_source_detailed(url)  # type: ignore[arg-type]
    assert result.source_type is SourceType.UNKNOWN
    assert result.rule_id == "invalid_url"


def test_no_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("classification must not touch the network")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert classify_source("https://docs.python.org/3/") is SourceType.DOCS


def test_custom_rules_extend_without_touching_the_table() -> None:
    extra = Rule("vendor_official", SourceType.OFFICIAL, host_suffixes=("example-vendor.test",))
    rules = (extra, *RULES)
    assert classify_source("https://docs.example-vendor.test/x", rules=rules) is SourceType.OFFICIAL
    assert classify_source("https://docs.example-vendor.test/x") is SourceType.DOCS


def test_deterministic() -> None:
    urls = [case["url"] for case in CASES]
    assert [classify_source(u) for u in urls] == [classify_source(u) for u in urls]


def test_very_long_path_is_handled() -> None:
    url = "https://example.com/" + "/".join(["a"] * 600)
    assert classify_source(url) is SourceType.UNKNOWN
