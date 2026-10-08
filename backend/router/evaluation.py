"""Router evaluation: diagnostics on an artificial set, never a quality pass.

A completed run reports how a router behaved on the labelled turns. It does not
establish quality and there is no pass threshold.
"""

import hashlib
import json
import re
from dataclasses import dataclass

from backend.router.contract import (
    Route,
    RouteDecision,
    Router,
    RouteReason,
    fallback,
)

ROUTES = tuple(route.value for route in Route)
KINDS = ("casual", "memory", "research", "ambiguous", "paraphrase", "adversarial")
MAX_TURNS = 500
MAX_TURN_CHARS = 5000
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


class RouterEvaluationDataError(ValueError):
    """The router gold set does not satisfy the versioned schema."""


@dataclass(frozen=True)
class RouterEvaluationDataset:
    identifier: str
    digest: str
    cost_matrix: dict
    turns: tuple[dict, ...]


class ContractOnlyRouter:
    """Always falls back. Proves the plumbing only; implies no routing quality."""

    async def decide(self, text: str) -> RouteDecision:
        return fallback(RouteReason.no_model)


def _route(value, *, allow_none=False):
    if allow_none and value is None:
        return None
    if not isinstance(value, str) or value not in ROUTES:
        raise RouterEvaluationDataError("Expected a known route")
    return value


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RouterEvaluationDataError("Expected bounded nonempty text")
    return value


def _cost_matrix(raw):
    if not isinstance(raw, dict):
        raise RouterEvaluationDataError("Missing cost matrix")
    matrix = {}
    for expected in ROUTES:
        row = raw.get(expected)
        if not isinstance(row, dict) or set(row) != set(ROUTES):
            raise RouterEvaluationDataError("Cost matrix must cover every route pair")
        for predicted, cost in row.items():
            if isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0:
                raise RouterEvaluationDataError("Costs must be non-negative numbers")
            if (expected == predicted) != (cost == 0):
                raise RouterEvaluationDataError("Only a correct route may cost zero")
        matrix[expected] = dict(row)
    return matrix


def parse_router_dataset(raw: dict) -> RouterEvaluationDataset:
    """Validate the gold set before any router is called."""
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise RouterEvaluationDataError("Unsupported evaluation schema")
    if isinstance(raw["schema_version"], bool) or raw.get("synthetic") is not True:
        raise RouterEvaluationDataError("Evaluation requires an explicitly synthetic corpus")
    identifier = raw.get("id")
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        raise RouterEvaluationDataError("Expected a stable dataset identifier")
    header = raw.get("header")
    if not isinstance(header, dict):
        raise RouterEvaluationDataError("Missing header")
    matrix = _cost_matrix(header.get("cost_matrix"))
    turns = raw.get("turns")
    if not isinstance(turns, list) or not 1 <= len(turns) <= MAX_TURNS:
        raise RouterEvaluationDataError("Expected a bounded list of turns")
    seen = set()
    for turn in turns:
        if not isinstance(turn, dict):
            raise RouterEvaluationDataError("Invalid turn")
        key = turn.get("id")
        if not isinstance(key, str) or not _ID.fullmatch(key) or key in seen:
            raise RouterEvaluationDataError("Duplicate or invalid turn ID")
        seen.add(key)
        if turn.get("kind") not in KINDS:
            raise RouterEvaluationDataError("Unknown turn kind")
        _text(turn.get("text"), MAX_TURN_CHARS)
        _text(turn.get("cost_of_wrong"), 500)
        expected = _route(turn.get("expected_route"))
        acceptable = turn.get("acceptable_routes")
        if (
            not isinstance(acceptable, list)
            or not acceptable
            or len(set(acceptable)) != len(acceptable)
        ):
            raise RouterEvaluationDataError("acceptable_routes must be a unique list")
        for route in acceptable:
            _route(route)
        if expected not in acceptable:
            raise RouterEvaluationDataError("The expected route must be acceptable")
        if turn["kind"] in ROUTES and (acceptable != [expected] or expected != turn["kind"]):
            raise RouterEvaluationDataError("Plain class turns have one acceptable route")
        weight = turn.get("cost_weight")
        if isinstance(weight, bool) or not isinstance(weight, int | float) or weight <= 0:
            raise RouterEvaluationDataError("cost_weight must be positive")
        steer = _route(turn.get("steer_target"), allow_none=True)
        if "steer_target" in turn and turn["kind"] != "adversarial":
            raise RouterEvaluationDataError("Only adversarial turns have a steer target")
        if turn["kind"] == "adversarial" and "steer_target" not in turn:
            raise RouterEvaluationDataError("Adversarial turns declare steer_target (or null)")
        if steer is not None and steer in acceptable:
            raise RouterEvaluationDataError("A steer target must not be an acceptable route")
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    copied = json.loads(payload)  # later caller mutation must not change the labels
    return RouterEvaluationDataset(
        identifier,
        hashlib.sha256(payload.encode()).hexdigest(),
        matrix,
        tuple(copied["turns"]),
    )


def _rate(count, total):
    return round(count / total, 4) if total else None


async def _decide(router, text):
    """Returns (decision, contract_violation). A raising router is a violation."""
    try:
        decision = await router.decide(text)
    except Exception:
        return fallback(RouteReason.model_error), True
    if not isinstance(decision, RouteDecision):
        return fallback(RouteReason.invalid_output), True
    return decision, False


async def evaluate(
    router: Router,
    dataset: RouterEvaluationDataset,
    *,
    router_name: str,
    evidence_kind: str,
    limit: int | None = None,
) -> dict:
    turns = dataset.turns if limit is None else dataset.turns[:limit]
    confusion = {e: dict.fromkeys(ROUTES, 0) for e in ROUTES}
    per_class = {e: {"turns": 0, "accepted": 0, "exact": 0} for e in ROUTES}
    per_kind = {k: {"turns": 0, "accepted": 0} for k in KINDS}
    fallback_reasons: dict[str, int] = {}
    pair_cost = {f"{e}->{p}": 0.0 for e in ROUTES for p in ROUTES if e != p}
    steering_ids, results = [], []
    steering_candidates = fallbacks = violations = unfallen_accepted = 0
    total_cost = 0.0
    for turn in turns:
        decision, violated = await _decide(router, turn["text"])
        violations += violated
        expected, predicted = turn["expected_route"], decision.route.value
        accepted = predicted in turn["acceptable_routes"]
        confusion[expected][predicted] += 1
        per_class[expected]["turns"] += 1
        per_class[expected]["accepted"] += accepted
        per_class[expected]["exact"] += predicted == expected
        per_kind[turn["kind"]]["turns"] += 1
        per_kind[turn["kind"]]["accepted"] += accepted
        unfallen_accepted += accepted and not decision.used_fallback
        if decision.used_fallback:
            fallbacks += 1
            fallback_reasons[decision.reason.value] = (
                fallback_reasons.get(decision.reason.value, 0) + 1
            )
        steered = False
        if turn["kind"] == "adversarial" and turn.get("steer_target") is not None:
            steering_candidates += 1
            # A safe fallback to the target route is not obedience to the injection.
            if predicted == turn["steer_target"] and not decision.used_fallback:
                steered = True
                steering_ids.append(turn["id"])
        cost = 0.0
        if not accepted:
            cost = dataset.cost_matrix[expected][predicted] * turn["cost_weight"]
            total_cost += cost
            pair_cost[f"{expected}->{predicted}"] += cost
        results.append(
            {
                "id": turn["id"],
                "kind": turn["kind"],
                "expected_route": expected,
                "predicted_route": predicted,
                "reason": decision.reason.value,
                "used_fallback": decision.used_fallback,
                "confidence": decision.confidence,
                "accepted": accepted,
                "steered": steered,
                "cost": cost,
            }
        )
    total = len(turns)
    accepted_total = sum(row["accepted"] for row in per_class.values())
    return {
        "schema_version": 1,
        "status": "completed",
        "quality_assessment": "not_established",
        "pass_threshold": None,
        "evidence_kind": evidence_kind,
        "dataset": {"id": dataset.identifier, "sha256": dataset.digest, "turns": total},
        "router": {"name": router_name},
        "summary": {
            "turns": total,
            "accepted": accepted_total,
            "accuracy": _rate(accepted_total, total),
            "accepted_without_fallback": unfallen_accepted,
            "accuracy_without_fallback": _rate(unfallen_accepted, total),
            "fallbacks": fallbacks,
            "fallback_rate": _rate(fallbacks, total),
            "fallback_by_reason": dict(sorted(fallback_reasons.items())),
            "router_contract_violations": violations,
            "adversarial": {
                "steering_candidates": steering_candidates,
                "steering_failures": len(steering_ids),
                "steering_failure_ids": steering_ids,
            },
            "cost_weighted_mistakes": total_cost,
            "cost_by_pair": {pair: cost for pair, cost in pair_cost.items() if cost},
        },
        "per_class": {
            e: {**v, "accuracy": _rate(v["accepted"], v["turns"])} for e, v in per_class.items()
        },
        "per_kind": {
            k: {**v, "accuracy": _rate(v["accepted"], v["turns"])} for k, v in per_kind.items()
        },
        "confusion_matrix": confusion,
        "results": results,
    }
