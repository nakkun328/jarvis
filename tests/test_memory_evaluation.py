"""Synthetic retrieval reports test contracts and canonical safety, not quality."""

import asyncio
import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.memory.embedding import EmbeddingSpace
from backend.memory.evaluation import (
    ContractOnlyEmbeddings,
    EvaluationDataError,
    _metrics,
    evaluate,
    evaluate_trials,
    parse_dataset,
)
from scripts.evaluate_memory import run, write_report

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/semantic-evaluation-ja-v1.json"


def raw_dataset():
    return json.loads(FIXTURE.read_text())


@pytest.mark.parametrize("damage", ["real", "duplicate", "pending-gold", "support", "replacement"])
def test_invalid_gold_is_rejected_before_model_allocation(damage):
    raw = raw_dataset()
    if damage == "real":
        raw["synthetic"] = False
    elif damage == "duplicate":
        raw["queries"].append(copy.deepcopy(raw["queries"][0]))
    elif damage == "pending-gold":
        raw["queries"][0]["relevant_ids"] = ["pending-runtime"]
    elif damage == "support":
        raw["queries"][0]["supporting_ids"] = ["lens-storage"]
    else:
        raw["notes"][0]["replacement_id"] = "absent"
    with pytest.raises(EvaluationDataError):
        parse_dataset(raw)


def test_label_copy_and_digest_ignore_json_key_order():
    raw = raw_dataset()
    dataset = parse_dataset(raw)
    reordered = json.loads(json.dumps(raw, sort_keys=True))
    assert parse_dataset(reordered).digest == dataset.digest
    raw["notes"][0]["content"] = "changed"
    assert dataset.notes[0]["content"] != "changed"
    assert parse_dataset(raw).digest != dataset.digest


def test_rank_metrics_and_evidence_gap_have_separate_denominators():
    result = _metrics(["noise", "a", "b"], ["a", "b", "c"], ["b"], 3)
    assert result["precision_at_k"] == pytest.approx(2 / 3)
    assert result["recall_at_k"] == pytest.approx(2 / 3)
    assert result["reciprocal_rank"] == 0.5
    assert result["support_recall_at_k"] == 1
    unsupported = _metrics(["a"], ["a"], [], 3)
    assert unsupported["recall_at_k"] == 1
    assert unsupported["support_recall_at_k"] is None
    assert unsupported["supporting_gold_exists"] is False
    negative = _metrics(["noise"], [], [], 3)
    assert negative["precision_at_k"] is None
    assert negative["negative_query_nonempty"] is True


def test_real_index_excludes_poisoned_ids_and_resolves_current_canonical_notes(monkeypatch):
    dataset = parse_dataset(raw_dataset())
    from backend.memory.chroma import ChromaVectorIndex

    paths = []

    def isolated_index(path):
        paths.append(path)
        return ChromaVectorIndex(path)

    monkeypatch.setattr("backend.memory.evaluation.ChromaVectorIndex", isolated_index)
    repeated = asyncio.run(
        evaluate_trials(ContractOnlyEmbeddings(), dataset, trials=2, evidence_kind="fake", limit=6)
    )
    first, second = repeated["trials"]
    assert len(paths) == len(set(paths)) == 2
    assert all(not p.exists() for p in paths)
    assert repeated["summary"]["completed_trials"] == 2
    assert repeated["quality_assessment"] == "not_established"
    assert first["status"] == second["status"] == "completed"
    assert first["quality_assessment"] == "not_established"
    assert first["quality_thresholds"] is None
    assert first["contract"]["identifier"] == "fixture/contract-only@v1:d8"
    assert first["summary"]["exclusion_violations"] == 0
    challenge = first["index_challenge"]
    assert set(challenge["extra_ids"]) == {
        "robot-old",
        "retired-venue",
        "pending-runtime",
        "pending-paint",
        "conflict-time",
    }
    assert challenge["stale_ids"] == ["observing-time"]
    assert challenge["original_notes_preserved"]
    # ANN can omit a candidate between fresh indexes: that is measured recall,
    # not permission to accept inactive notes or claim a model quality pass.
    approved = {n["id"]: n for n in dataset.notes if n["status"] == "approved"}
    for report in (first, second):
        assert report["summary"]["exclusion_violations"] == 0
        for row in report["queries"]:
            assert 0 < len(row["retrieved_ids"]) <= 6
            assert not row["forbidden_ids_returned"]
            assert set(row["retrieved_ids"]) <= set(approved)
            for value in row["results"]:
                note = approved[value["id"]]
                assert value["content"] == note.get("edit_to", note["content"])
                assert value["edited_since_approval"] == ("edit_to" in note)
                assert value["origin"] == "user_explicit"
                assert value["confidence"] == 0.8
                assert value["source"].startswith("synthetic-evaluation:")
    assert first["dataset"] == second["dataset"]
    assert first["contract"] == second["contract"]
    assert first["retrieval_environment"] == second["retrieval_environment"]
    assert [
        {k: row[k] for k in ("id", "kind", "text", "rationale", "relevant_ids", "supporting_ids")}
        for row in first["queries"]
    ] == list(dataset.queries)
    assert first["summary"]["negative_nonempty"] == 3
    assert first["summary"]["insufficient_evidence_queries"] == 2
    assert first["summary"]["insufficient_evidence_returning_context"] == 2


@pytest.mark.parametrize("output", [[], [(float("nan"),)], [(1.0,)]])
def test_invalid_embedding_output_fails_without_quality_claim_or_raw_error(output):
    class Broken:
        space = EmbeddingSpace("fixture/broken", "v1", 2)

        async def embed(self, texts):
            return output * len(texts)

    report = asyncio.run(evaluate(Broken(), parse_dataset(raw_dataset()), evidence_kind="fake"))
    assert report["status"] == "failed"
    assert report["error"]["stage"] == "index_build"
    assert report["queries"] == []
    assert report["quality_assessment"] == "not_established"


@pytest.mark.parametrize("trials", [1, 2])
def test_external_factory_provider_is_closed_after_private_failure(monkeypatch, trials):
    class Provider:
        space = EmbeddingSpace("fixture/private-error", "v2", 2)
        closed = False
        close_count = 0

        async def embed(self, texts):
            raise RuntimeError("private sentinel: body, credential and path")

        async def aclose(self):
            self.closed = True
            self.close_count += 1

    provider = Provider()
    monkeypatch.setitem(
        sys.modules, "evaluation_test_adapter", SimpleNamespace(factory=lambda: provider)
    )
    args = SimpleNamespace(
        fixture=FIXTURE,
        provider_factory="evaluation_test_adapter:factory",
        evidence_kind="fake",
        limit=3,
        trials=trials,
    )
    report = asyncio.run(run(args))
    assert provider.closed
    assert provider.close_count == 1
    assert report["status"] == "failed"
    assert (report if trials == 1 else report["trials"][0])["error"]["stage"] == "index_build"
    if trials > 1:
        assert report["summary"]["failed_trials"] == trials
    assert "private sentinel" not in json.dumps(report)
    assert report["contract"]["identifier"] == "fixture/private-error@v2:d2"
    assert report["runner"]["implementation_sha256"]


@pytest.mark.parametrize("trials", [1, 3])
def test_cancel_propagates_and_owned_provider_closes(monkeypatch, trials):
    class Provider:
        space = EmbeddingSpace("fixture/cancel", "v1", 2)
        closed = False

        async def embed(self, texts):
            raise asyncio.CancelledError

        async def aclose(self):
            self.closed = True

    provider = Provider()
    monkeypatch.setitem(
        sys.modules, "evaluation_test_adapter", SimpleNamespace(factory=lambda: provider)
    )
    args = SimpleNamespace(
        fixture=FIXTURE,
        provider_factory="evaluation_test_adapter:factory",
        evidence_kind="fake",
        limit=3,
        trials=trials,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run(args))
    assert provider.closed


def test_report_is_atomic_new_file_and_rejects_symlink(tmp_path):
    report = tmp_path / "new.json"
    write_report(report, {"text": "人工データ"})
    original = report.read_bytes()
    with pytest.raises(FileExistsError):
        write_report(report, {"text": "changed"})
    assert report.read_bytes() == original
    link = tmp_path / "symlink.json"
    link.symlink_to(report)
    with pytest.raises(FileExistsError):
        write_report(link, {})
    assert report.read_bytes() == original
    assert set(tmp_path.iterdir()) == {report, link}


def test_cli_records_execution_evidence_without_quality_pass_and_refuses_overwrite(tmp_path):
    report = tmp_path / "report.json"
    command = [
        sys.executable,
        str(ROOT / "scripts/evaluate_memory.py"),
        "--trials",
        "2",
        "--output",
        str(report),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(report.read_text())
    assert result["status"] == "completed"
    assert result["quality_assessment"] == "not_established"
    assert result["dataset"]["queries"] == 12
    assert result["summary"]["requested_trials"] == result["summary"]["completed_trials"] == 2
    assert result["fixture_file_sha256"]
    assert "quality not established" in completed.stdout
    before = report.read_bytes()
    rejected = subprocess.run(command, capture_output=True, text=True)
    assert rejected.returncode == 2
    assert report.read_bytes() == before


def test_contract_vectors_cannot_be_relabeled_model_quality():
    with pytest.raises(EvaluationDataError):
        asyncio.run(
            evaluate(ContractOnlyEmbeddings(), parse_dataset(raw_dataset()), evidence_kind="model")
        )


def test_partial_report_preserves_completed_queries_and_sanitizes_failure():
    dataset = parse_dataset(raw_dataset())

    class Provider(ContractOnlyEmbeddings):
        async def embed(self, texts):
            if tuple(texts) == (dataset.queries[1]["text"],):
                raise RuntimeError("private-provider-payload")
            return await super().embed(texts)

    report = asyncio.run(evaluate(Provider(), dataset, evidence_kind="fake"))
    assert report["status"] == "failed"
    assert len(report["queries"]) == 1
    assert report["error"]["stage"] == "query:" + dataset.queries[1]["id"]
    assert "summary" not in report
    assert "private-provider-payload" not in json.dumps(report)


def test_invalid_fixture_does_not_construct_provider(tmp_path, monkeypatch):
    fixture = tmp_path / "invalid.json"
    raw = raw_dataset()
    raw["synthetic"] = False
    fixture.write_text(json.dumps(raw))
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "evaluation_test_adapter",
        SimpleNamespace(factory=lambda: calls.append("created")),
    )
    args = SimpleNamespace(
        fixture=fixture,
        provider_factory="evaluation_test_adapter:factory",
        evidence_kind="fake",
        limit=3,
    )
    report = asyncio.run(run(args))
    assert not calls
    assert report["status"] == "failed"
    assert report["error"]["stage"] == "dataset_validation"


@pytest.mark.parametrize("trials", [0, 21, True, 1.5])
def test_trial_count_is_finite_and_rejected_before_any_provider_call(trials):
    class Provider(ContractOnlyEmbeddings):
        async def embed(self, texts):
            pytest.fail("Invalid trial count must not execute")

    with pytest.raises(EvaluationDataError):
        asyncio.run(
            evaluate_trials(
                Provider(), parse_dataset(raw_dataset()), trials=trials, evidence_kind="fake"
            )
        )


def test_trial_reports_membership_rank_distributions_and_partial_failures(monkeypatch):
    from backend.memory.evaluation import _metrics

    raw = raw_dataset()
    raw["queries"] = [raw["queries"][0]]
    dataset = parse_dataset(raw)
    gold = copy.deepcopy(dataset.queries[0])
    good, noise, other = "robot-current", "lens-storage", "data-format"
    rankings = [[good, noise], [noise, good], [good, other]]
    calls = []

    async def controlled(provider, given, **kwargs):
        # A provider's caller mutating the original cannot change the frozen
        # dataset/gold for the remaining trials.
        assert given.queries[0]["text"] == gold["text"]
        calls.append(given)
        if len(calls) == 1:
            dataset.queries[0]["text"] = "Caller mutation between trials"
        row = {
            **dict(gold),
            "retrieved_ids": rankings[len(calls) - 1] if len(calls) <= 3 else [],
            "forbidden_ids_returned": [],
        }
        row["metrics"] = _metrics(
            row["retrieved_ids"], gold["relevant_ids"], gold["supporting_ids"], 2
        )
        if len(calls) == 4:
            row["forbidden_ids_returned"] = ["robot-old"]
            return {
                "status": "failed",
                "queries": [row],
                "error": {"stage": "query", "message": "Sanitized failure"},
            }
        return {
            "status": "completed",
            "queries": [row],
            "summary": {
                "mean_precision_at_k": row["metrics"]["precision_at_k"],
                "mean_reciprocal_rank": row["metrics"]["reciprocal_rank"],
            },
        }

    monkeypatch.setattr("backend.memory.evaluation.evaluate", controlled)
    report = asyncio.run(
        evaluate_trials(ContractOnlyEmbeddings(), dataset, trials=4, evidence_kind="fake", limit=2)
    )
    assert report["status"] == "failed"
    assert report["safety_status"] == "violated"
    summary = report["summary"]
    assert summary["completed_trials"] == 3 and summary["failed_trials"] == 1
    assert summary["excluded_trial_numbers"] == [4]
    assert summary["exclusion_violations"] == 1
    assert summary["metric_distributions"]["mean_reciprocal_rank"]["values"] == [1, 0.5, 1]
    assert summary["metric_distributions"]["mean_reciprocal_rank"]["mean"] == pytest.approx(5 / 6)
    assert summary["metric_distributions"]["mean_reciprocal_rank"]["population_stdev"] > 0
    variation = report["queries"][0]
    assert variation["candidate_set_variants"] == 2
    assert variation["ranking_variants"] == 3
    assert variation["ranks_by_candidate"][good] == [1, 2, 1]
    assert variation["ranks_by_candidate"][noise] == [2, 1, None]
    assert variation["observed_trial_numbers"] == [1, 2, 3]
    assert variation["text"] == gold["text"]
    assert report["trials"][3]["queries"][0]["forbidden_ids_returned"] == ["robot-old"]
    assert report["quality_thresholds"] is None


def test_changed_provider_contract_is_not_averaged_or_called_again(monkeypatch):
    class Provider(ContractOnlyEmbeddings):
        pass

    provider = Provider()
    calls = []

    async def changed(provider, dataset, **kwargs):
        calls.append(1)
        provider.space = EmbeddingSpace("fixture/changed", "v2", 8)
        return {"status": "completed", "queries": [], "summary": {}}

    monkeypatch.setattr("backend.memory.evaluation.evaluate", changed)
    report = asyncio.run(
        evaluate_trials(provider, parse_dataset(raw_dataset()), trials=3, evidence_kind="fake")
    )
    assert calls == [1]
    assert report["status"] == "failed"
    assert report["summary"]["completed_trials"] == 0
    assert report["summary"]["executed_trials"] == 1
    assert report["summary"]["failed_trials"] == 3
    assert report["summary"]["metric_distributions"] == {}
    assert report["queries"][0]["ranking_changed"] is None
    assert report["safety_status"] == "incomplete"
