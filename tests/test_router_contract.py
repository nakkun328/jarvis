"""Router contract, rule baseline and audit: fixed vocabulary, safe fallback, no text."""

import asyncio
import dataclasses
import hashlib
import logging

import pytest

from backend.router import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    FALLBACK_REASONS,
    AuditedRouter,
    InMemoryAuditSink,
    Route,
    RouteAuditRecord,
    RouteDecision,
    Router,
    RouterContractError,
    RouteReason,
    RuleRouter,
    fallback,
)
from backend.router.audit import confidence_bucket, input_digest, length_bucket


def decision(**changes):
    values = {
        "route": Route.casual,
        "confidence": 0.9,
        "reason": RouteReason.model_choice,
        "used_fallback": False,
    }
    return RouteDecision(**{**values, **changes})


def test_vocabulary_is_fixed():
    assert {r.value for r in Route} == {"casual", "memory", "research"}
    assert DEFAULT_CONFIDENCE_THRESHOLD == 0.6
    assert RouteReason.model_choice not in FALLBACK_REASONS
    assert RouteReason.rule_match not in FALLBACK_REASONS
    assert {
        "low_confidence",
        "invalid_output",
        "model_error",
        "timeout",
        "empty_input",
        "input_too_long",
    } <= {r.value for r in FALLBACK_REASONS}


def test_valid_decision_is_frozen_and_normalised():
    value = decision(confidence=1)
    assert value.confidence == 1.0 and isinstance(value.confidence, float)
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.route = Route.research


@pytest.mark.parametrize(
    "changes",
    [
        {"route": "casual"},
        {"reason": "model_choice"},
        {"confidence": True},
        {"confidence": "0.9"},
        {"confidence": None},
        {"confidence": -0.01},
        {"confidence": 1.01},
        {"confidence": float("nan")},
        {"confidence": float("inf")},
        {"used_fallback": 1},
        {"used_fallback": True},  # model_choice is not a fallback reason
        {"reason": RouteReason.timeout},  # fallback reason without used_fallback
        {"route": Route.casual, "reason": RouteReason.timeout, "used_fallback": True},
        {"route": Route.research, "reason": RouteReason.invalid_output, "used_fallback": True},
    ],
)
def test_invalid_decisions_are_rejected(changes):
    with pytest.raises(RouterContractError):
        decision(**changes)


@pytest.mark.parametrize("reason", sorted(FALLBACK_REASONS))
def test_fallback_is_the_main_agent_path(reason):
    value = fallback(reason)
    assert value.route is Route.memory and value.used_fallback and value.reason is reason


# --- RuleRouter -------------------------------------------------------------------


def route_of(router, text):
    return asyncio.run(router.decide(text))


@pytest.mark.parametrize(
    ("text", "route"),
    [
        ("こんにちは！", Route.casual),
        ("今日はちょっと疲れたなあ", Route.casual),
        ("前に話した旅行の計画、どうなったっけ？", Route.memory),
        ("私の好みに合う映画を教えて", Route.memory),
        ("今日のニュースを教えて", Route.research),
        ("東京と大阪の人口を比較して", Route.research),
    ],
)
def test_rule_router_matches_keywords(text, route):
    result = route_of(RuleRouter(), text)
    assert (result.route, result.reason, result.used_fallback) == (
        route,
        RouteReason.rule_match,
        False,
    )


def test_rule_router_falls_back_without_a_match_or_on_conflict():
    router = RuleRouter()
    unmatched = route_of(router, "二つの選択肢のどちらが得か、客観的な数字で見たい")
    assert (unmatched.route, unmatched.reason) == (Route.memory, RouteReason.no_match)
    conflict = route_of(router, "こんにちは、最新のニュースを教えて")
    assert (conflict.route, conflict.reason, conflict.used_fallback) == (
        Route.memory,
        RouteReason.low_confidence,
        True,
    )


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", RouteReason.empty_input),
        ("   \n", RouteReason.empty_input),
        (None, RouteReason.empty_input),
        (123, RouteReason.empty_input),
        ("あ" * 2001, RouteReason.input_too_long),
    ],
)
def test_rule_router_never_raises_on_bad_input(text, reason):
    result = route_of(RuleRouter(), text)
    assert result.reason is reason and result.used_fallback and result.route is Route.memory


def test_routers_satisfy_the_protocol():
    assert isinstance(RuleRouter(), Router)


# --- Audit ------------------------------------------------------------------------


def test_buckets():
    assert [confidence_bucket(x) for x in (0.0, 0.09, 0.1, 0.6, 0.99, 1.0)] == [0, 0, 1, 6, 9, 10]
    assert [length_bucket(n) for n in (0, 1, 19, 20, 99, 100, 499, 500, 1999, 2000, 99999)] == [
        "0",
        "1-19",
        "1-19",
        "20-99",
        "20-99",
        "100-499",
        "100-499",
        "500-1999",
        "500-1999",
        "2000+",
        "2000+",
    ]


SECRET_TEXT = "私の秘密の合言葉は青いペンギンです 前に話した"


def test_audit_record_has_no_text_and_a_sha256_digest():
    sink = InMemoryAuditSink()
    router = AuditedRouter(RuleRouter(), sink)
    result = asyncio.run(router.decide(SECRET_TEXT))
    (record,) = sink.records
    assert record.route is result.route is Route.memory
    assert record.reason is RouteReason.rule_match and record.used_fallback is False
    assert record.confidence_bucket == 8 and record.length_bucket == "20-99"
    assert record.input_sha256 == hashlib.sha256(SECRET_TEXT.encode()).hexdigest()
    assert SECRET_TEXT not in repr(record) and "ペンギン" not in repr(record)
    assert {f.name for f in dataclasses.fields(record)} == {
        "route",
        "reason",
        "used_fallback",
        "confidence_bucket",
        "length_bucket",
        "input_sha256",
    }


def test_audit_logs_only_fixed_event_without_text(caplog):
    caplog.set_level(logging.DEBUG, logger="backend.router")
    asyncio.run(AuditedRouter(RuleRouter(), InMemoryAuditSink()).decide(SECRET_TEXT))
    assert [r.getMessage() for r in caplog.records if r.name.startswith("backend.router")] == [
        "router.decision"
    ]
    assert "ペンギン" not in str([r.__dict__ for r in caplog.records])


def test_audit_failure_never_changes_the_decision(caplog):
    class BrokenSink:
        def record(self, record):
            raise RuntimeError(SECRET_TEXT)

    caplog.set_level(logging.DEBUG, logger="backend.router")
    result = asyncio.run(AuditedRouter(RuleRouter(), BrokenSink()).decide("こんにちは"))
    assert result.route is Route.casual
    assert [r.getMessage() for r in caplog.records if r.name.startswith("backend.router")] == [
        "router.audit_failed"
    ]
    assert "ペンギン" not in str([r.__dict__ for r in caplog.records])


def test_audit_digest_handles_odd_input_and_record_validation():
    assert input_digest("\ud800") == hashlib.sha256(b"?").hexdigest()
    assert input_digest(None) == hashlib.sha256(b"").hexdigest()
    good = RouteAuditRecord.from_decision("x", fallback(RouteReason.timeout))
    for changes in (
        {"confidence_bucket": 11},
        {"confidence_bucket": True},
        {"length_bucket": "5"},
        {"input_sha256": "abc"},
        {"input_sha256": "Z" * 64},
        {"route": "memory"},
    ):
        with pytest.raises(RouterContractError):
            dataclasses.replace(good, **changes)


def test_in_memory_sink_is_bounded():
    sink = InMemoryAuditSink(maximum=2)
    record = RouteAuditRecord.from_decision("x", fallback(RouteReason.timeout))
    for _ in range(5):
        sink.record(record)
    assert len(sink.records) == 2
    with pytest.raises(ValueError):
        InMemoryAuditSink(maximum=0)
