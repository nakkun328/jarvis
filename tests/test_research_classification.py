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


@pytest.mark.parametrize(
    ("url", "source_type", "rule", "basis"),
    [
        ("https://ai.google.dev/pricing", SourceType.DOCS, "vendor_docs_host", Basis.HOST),
        (
            "https://platform.openai.com/docs/models",
            SourceType.DOCS,
            "vendor_docs_host",
            Basis.HOST,
        ),
        ("https://console.groq.com/docs/models", SourceType.DOCS, "vendor_docs_host", Basis.HOST),
        (
            "https://learn.microsoft.com/en-us/azure/x",
            SourceType.DOCS,
            "vendor_docs_host",
            Basis.HOST,
        ),
        ("https://developers.openai.com/api/docs", SourceType.DOCS, "docs_host", Basis.HOST),
        (
            "https://cloud.google.com/vertex-ai/generative-ai/pricing",
            SourceType.DOCS,
            "vendor_docs_path",
            Basis.PATH,
        ),
        ("https://cloud.google.com/blog/products/x", SourceType.BLOG, "blog_path", Basis.PATH),
        ("https://cloud.google.com/about", SourceType.UNKNOWN, "no_rule", Basis.DEFAULT),
        (
            "https://github.com/org/repo/blob/main/README.md",
            SourceType.DOCS,
            "github_docs_path",
            Basis.PATH,
        ),
        (
            "https://github.com/org/repo/issues/3",
            SourceType.COMMUNITY,
            "community_path",
            Basis.PATH,
        ),
        (
            "https://docsbot.ai/models/compare",
            SourceType.UNKNOWN,
            "comparison_site_host",
            Basis.HOST,
        ),
        ("https://benchlm.ai/models", SourceType.UNKNOWN, "benchmark_site_host", Basis.HOST),
        ("https://openrouter.ai/models", SourceType.UNKNOWN, "aggregator_host", Basis.HOST),
    ],
)
def test_vendor_docs_and_comparison_sites(url: str, source_type, rule: str, basis) -> None:
    result = classify_source_detailed(url)
    assert (result.source_type, result.rule_id, result.basis) == (source_type, rule, basis)


@pytest.mark.parametrize(
    "url",
    [
        "https://ai.google.dev.evil.example/pricing",
        "https://evilai.google.dev.example/",
        "https://notopenrouter.ai/models",
        "https://openrouter.ai.evil.example/models",
        "https://example.com/?u=platform.openai.com",
        "https://example.com/cloud.google.com/pricing",
        "https://cloud.google.com.evil.example/pricing",
        "https://fake-cloud.google.com.example/docs",
    ],
)
def test_vendor_and_site_rules_are_not_substring_matches(url: str) -> None:
    assert classify_source_detailed(url).rule_id in {"no_rule", "docs_path"}
    assert classify_source(url) in {SourceType.UNKNOWN, SourceType.DOCS}


def test_comparison_sites_do_not_gain_authority() -> None:
    from backend.research.evaluation import authority_rating

    result = classify_source_detailed("https://docsbot.ai/x")
    authority, _ = authority_rating(result.source_type, result.basis)
    assert authority == authority_rating(SourceType.UNKNOWN)[0]
    # a path-based vendor docs decision stays capped below a host-table one
    path = classify_source_detailed("https://cloud.google.com/pricing")
    assert authority_rating(path.source_type, path.basis)[0] < authority_rating(SourceType.DOCS)[0]


# ----- subject_official_host -----


@pytest.mark.parametrize(
    ("url", "kind", "rule"),
    [
        ("https://www.sqlite.org/wal.html", SourceType.DOCS, "subject_official_host"),
        ("https://sqlite.org/howtocorrupt.html", SourceType.DOCS, "subject_official_host"),
        ("https://www.postgresql.org/about/", SourceType.DOCS, "subject_official_host"),
        ("https://sqlite.org/forum/forumpost/abc", SourceType.FORUM, "forum_path"),
        ("https://sqlite.org/blog/post", SourceType.BLOG, "blog_path"),
        ("https://blog.sqlite.org/x", SourceType.BLOG, "blog_host"),
        ("https://sqlite.org/docs/index", SourceType.DOCS, "docs_path"),
        # not a whole-label match of the registrable domain
        ("https://sqlite.org.evil.example/wal.html", SourceType.UNKNOWN, "no_rule"),
        ("https://sqlite.evil.com/wal.html", SourceType.UNKNOWN, "no_rule"),
        ("https://mysqlite.org/wal.html", SourceType.UNKNOWN, "no_rule"),
        ("https://sqlite-docs.org/wal.html", SourceType.UNKNOWN, "no_rule"),
        ("https://evil.example/sqlite.org/wal.html", SourceType.UNKNOWN, "no_rule"),
        # anyone can publish on these, whatever the question says
        ("https://github.com/someone/repo", SourceType.UNKNOWN, "no_rule"),
    ],
)
def test_subject_official_host(url: str, kind: SourceType, rule: str) -> None:
    result = classify_source_detailed(
        url, subject_terms={"sqlite", "postgresql", "wal", "github"}
    )
    assert (result.source_type, result.rule_id) == (kind, rule)


def test_subject_official_needs_the_terms_and_keeps_existing_callers_working() -> None:
    assert classify_source_detailed("https://www.sqlite.org/wal.html").rule_id == "no_rule"
    empty = classify_source_detailed("https://www.sqlite.org/wal.html", subject_terms=())
    assert empty.rule_id == "no_rule"
    # case-insensitive and plural-insensitive on the term side
    folded = classify_source_detailed("https://sqlite.org/a.html", subject_terms={"SQLite"})
    assert folded.rule_id == "subject_official_host"
