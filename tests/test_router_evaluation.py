"""Router evaluation reports behaviour on artificial turns, never a quality pass."""

import asyncio
import copy
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.router import Route, RouteDecision, RouteReason, RuleRouter, fallback
from backend.router.evaluation import (
    ContractOnlyRouter,
    RouterEvaluationDataError,
    evaluate,
    parse_router_dataset,
)
from scripts.evaluate_router import run, write_report

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/router-eval-ja-v1.json"


def raw_dataset():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def evaluate_with(router, **kwargs):
    dataset = parse_router_dataset(raw_dataset())
    return asyncio.run(evaluate(router, dataset, router_name="t", evidence_kind="fake", **kwargs))


# --- dataset ----------------------------------------------------------------------


def test_fixture_is_a_balanced_artificial_set():
    raw = raw_dataset()
    dataset = parse_router_dataset(raw)
    turns = dataset.turns
    assert raw["synthetic"] is True and len(turns) >= 80
    assert len({t["id"] for t in turns}) == len(turns)
    counts = Counter(t["expected_route"] for t in turns)
    assert set(counts) == {"casual", "memory", "research"}
    assert min(counts.values()) >= 25 and max(counts.values()) <= 1.5 * min(counts.values())
    kinds = Counter(t["kind"] for t in turns)
    assert set(kinds) == {"casual", "memory", "research", "ambiguous", "paraphrase", "adversarial"}
    assert kinds["adversarial"] >= 10 and kinds["ambiguous"] >= 8 and kinds["paraphrase"] >= 8
    adversarial = [t for t in turns if t["kind"] == "adversarial"]
    assert sum(t["steer_target"] is not None for t in adversarial) >= 8
    assert any(len(t["text"]) > 2000 for t in adversarial)  # exercises the length limit
    assert any(
        not t["text"].isascii() and any(c.isascii() and c.isalpha() for c in t["text"])
        for t in adversarial
    )  # mixed language
    assert all(t["cost_of_wrong"] for t in turns)
    assert {"casual", "memory", "research", "ambiguous", "paraphrase", "adversarial"} <= set(
        raw["header"]["kinds"]
    )
    assert set(raw["header"]["classes"]) == {"casual", "memory", "research"}


def test_fixture_has_no_secret_or_path_shaped_text():
    text = FIXTURE.read_text(encoding="utf-8")
    for needle in ("/Users/", "api_key", "password", "sk-"):
        assert needle not in text


@pytest.mark.parametrize(
    "damage",
    [
        "real",
        "schema",
        "duplicate",
        "route",
        "kind",
        "acceptable",
        "expected-not-acceptable",
        "plain-multi",
        "steer-acceptable",
        "steer-on-plain",
        "no-steer-key",
        "weight",
        "empty",
        "matrix-diagonal",
        "matrix-missing",
        "toolong",
    ],
)
def test_invalid_gold_is_rejected_before_any_router_runs(damage):
    raw = raw_dataset()
    plain = next(t for t in raw["turns"] if t["kind"] == "casual")
    adversarial = next(t for t in raw["turns"] if t["kind"] == "adversarial" and t["steer_target"])
    if damage == "real":
        raw["synthetic"] = False
    elif damage == "schema":
        raw["schema_version"] = 2
    elif damage == "duplicate":
        raw["turns"].append(copy.deepcopy(raw["turns"][0]))
    elif damage == "route":
        plain["expected_route"] = "chat"
    elif damage == "kind":
        plain["kind"] = "weird"
    elif damage == "acceptable":
        plain["acceptable_routes"] = []
    elif damage == "expected-not-acceptable":
        adversarial["acceptable_routes"] = (
            ["memory"] if adversarial["expected_route"] != "memory" else ["casual"]
        )
    elif damage == "plain-multi":
        plain["acceptable_routes"] = ["casual", "memory"]
    elif damage == "steer-acceptable":
        adversarial["acceptable_routes"] = [
            adversarial["expected_route"],
            adversarial["steer_target"],
        ]
    elif damage == "steer-on-plain":
        plain["steer_target"] = "research"
    elif damage == "no-steer-key":
        del adversarial["steer_target"]
    elif damage == "weight":
        plain["cost_weight"] = 0
    elif damage == "empty":
        plain["text"] = "  "
    elif damage == "matrix-diagonal":
        raw["header"]["cost_matrix"]["casual"]["casual"] = 1
    elif damage == "matrix-missing":
        del raw["header"]["cost_matrix"]["memory"]["research"]
    else:
        plain["text"] = "あ" * 5001
    with pytest.raises(RouterEvaluationDataError):
        parse_router_dataset(raw)


def test_digest_ignores_key_order_but_not_labels_and_copy_is_isolated():
    raw = raw_dataset()
    dataset = parse_router_dataset(raw)
    assert (
        parse_router_dataset(json.loads(json.dumps(raw, sort_keys=True))).digest == dataset.digest
    )
    raw["turns"][0]["text"] = "changed"
    assert dataset.turns[0]["text"] != "changed"
    assert parse_router_dataset(raw).digest != dataset.digest


# --- runner -----------------------------------------------------------------------


def test_contract_only_router_always_falls_back_and_claims_nothing():
    report = evaluate_with(ContractOnlyRouter())
    summary = report["summary"]
    assert report["quality_assessment"] == "not_established" and report["pass_threshold"] is None
    assert summary["fallback_rate"] == 1.0
    assert summary["fallback_by_reason"] == {"no_model": summary["turns"]}
    assert summary["accepted_without_fallback"] == 0
    assert summary["adversarial"]["steering_failures"] == 0
    assert all(row["predicted_route"] == "memory" for row in report["results"])
    assert all(
        report["confusion_matrix"][e]["casual"] == 0 == report["confusion_matrix"][e]["research"]
        for e in ("casual", "memory", "research")
    )


def test_rule_router_report_is_consistent_and_exposes_the_baseline_gap():
    report = evaluate_with(RuleRouter())
    summary, kinds = report["summary"], report["per_kind"]
    assert report["dataset"]["turns"] == summary["turns"] == len(report["results"]) >= 80
    assert sum(sum(row.values()) for row in report["confusion_matrix"].values()) == summary["turns"]
    assert summary["accepted"] == sum(r["accepted"] for r in report["results"])
    assert summary["cost_weighted_mistakes"] == sum(r["cost"] for r in report["results"])
    assert sum(summary["cost_by_pair"].values()) == summary["cost_weighted_mistakes"]
    assert summary["fallbacks"] == sum(r["used_fallback"] for r in report["results"])
    assert 0 < summary["fallback_rate"] < 1
    assert (
        kinds["paraphrase"]["accuracy"] < kinds["memory"]["accuracy"]
    )  # keywords miss paraphrases
    assert set(report["per_class"]) == {"casual", "memory", "research"}
    assert "text" not in report["results"][0]
    assert "ルーター" not in json.dumps(report, ensure_ascii=False)


def test_steering_failures_are_counted_against_the_injected_target():
    class Obedient:
        """Does whatever the injected text says: a deliberately broken router."""

        async def decide(self, text):
            for route in Route:
                if f"{route.value}と答え" in text or f"{route.value}を出力" in text:
                    return RouteDecision(route, 1.0, RouteReason.model_choice, False)
            return fallback(RouteReason.no_match)

    report = evaluate_with(Obedient())
    adversarial = report["summary"]["adversarial"]
    assert adversarial["steering_failures"] >= 2
    assert set(adversarial["steering_failure_ids"]) == {
        r["id"] for r in report["results"] if r["steered"]
    }
    assert adversarial["steering_failures"] <= adversarial["steering_candidates"]


def test_acceptable_ambiguous_routes_are_not_mistakes():
    class Always:
        def __init__(self, route):
            self.route = route

        async def decide(self, text):
            if self.route is Route.memory:
                return fallback(RouteReason.low_confidence, 0.1)
            return RouteDecision(self.route, 0.9, RouteReason.model_choice, False)

    for route in Route:
        report = evaluate_with(Always(route))
        for row in report["results"]:
            if row["kind"] == "ambiguous":
                expected = next(
                    t for t in parse_router_dataset(raw_dataset()).turns if t["id"] == row["id"]
                )
                assert row["accepted"] == (route.value in expected["acceptable_routes"])
                assert (row["cost"] > 0) == (not row["accepted"])


def test_cost_uses_matrix_and_weight():
    report = evaluate_with(ContractOnlyRouter())
    dataset = parse_router_dataset(raw_dataset())
    expected = sum(
        dataset.cost_matrix[t["expected_route"]]["memory"] * t["cost_weight"]
        for t in dataset.turns
        if "memory" not in t["acceptable_routes"]
    )
    assert report["summary"]["cost_weighted_mistakes"] == expected > 0


def test_a_contract_violating_router_is_counted_not_trusted():
    class Raises:
        async def decide(self, text):
            raise RuntimeError("boom")

    class Wrong:
        async def decide(self, text):
            return "research"

    for router in (Raises(), Wrong()):
        report = evaluate_with(router, limit=5)
        assert report["summary"]["turns"] == 5
        assert report["summary"]["router_contract_violations"] == 5
        assert all(
            r["predicted_route"] == "memory" and r["used_fallback"] for r in report["results"]
        )


# --- script -----------------------------------------------------------------------


def args(tmp_path, **changes):
    values = {
        "fixture": FIXTURE,
        "output": tmp_path / "report.json",
        "limit": None,
        "router": "rule",
        "router_factory": None,
        "evidence_kind": None,
    }
    return SimpleNamespace(**{**values, **changes})


def test_script_run_builds_a_full_report(tmp_path):
    report = asyncio.run(run(args(tmp_path)))
    assert report["status"] == "completed" and report["quality_assessment"] == "not_established"
    assert report["evidence_kind"] == "rule_baseline" and report["router"]["name"] == "rule"
    assert len(report["fixture_file_sha256"]) == 64 and len(report["dataset"]["sha256"]) == 64
    assert set(report["runner"]) == {"commit", "dirty", "script_sha256", "implementation_sha256"}
    contract = asyncio.run(run(args(tmp_path, router="contract-only")))
    assert contract["evidence_kind"] == "fake"


def test_script_reports_failure_without_details(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    report = asyncio.run(run(args(tmp_path, fixture=bad)))
    assert report["status"] == "failed" and report["quality_assessment"] == "not_established"
    assert report["error"] == {
        "stage": "dataset_validation",
        "type": "JSONDecodeError",
        "message": "Evaluation could not complete",
    }
    missing = asyncio.run(
        run(args(tmp_path, router_factory="no_such_module:x", evidence_kind="fake"))
    )
    assert missing["error"]["stage"] == "router_factory"


def test_report_write_is_atomic_and_never_overwrites(tmp_path):
    target = tmp_path / "nested" / "report.json"
    write_report(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert [p.name for p in target.parent.iterdir()] == ["report.json"]  # no temp left over
    with pytest.raises(FileExistsError):
        write_report(target, {"a": 2})
    assert json.loads(target.read_text()) == {"a": 1}
    link = tmp_path / "link.json"
    os.symlink(target, link)
    with pytest.raises(FileExistsError):
        write_report(link, {"a": 3})
    assert json.loads(target.read_text()) == {"a": 1}
    dangling = tmp_path / "dangling.json"
    os.symlink(tmp_path / "nowhere.json", dangling)
    with pytest.raises(FileExistsError):
        write_report(dangling, {"a": 4})
    assert not (tmp_path / "nowhere.json").exists()


def cli(tmp_path, *extra, output="out.json"):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/evaluate_router.py"),
            "--output",
            str(tmp_path / output),
            *extra,
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_cli_end_to_end_and_refuses_existing_output(tmp_path):
    first = cli(tmp_path, "--router", "contract-only")
    assert first.returncode == 0 and "quality not established" in first.stdout
    report = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert report["summary"]["fallback_rate"] == 1.0
    again = cli(tmp_path, "--router", "contract-only")
    assert again.returncode == 2 and "already exists" in again.stderr


def test_cli_rejects_unsafe_flag_combinations(tmp_path):
    assert cli(tmp_path, "--router-factory", "m:f", output="a.json").returncode == 2
    assert cli(tmp_path, "--evidence-kind", "model", output="b.json").returncode == 2
    assert cli(tmp_path, "--limit", "0", output="c.json").returncode == 2
    assert not list(tmp_path.iterdir())
