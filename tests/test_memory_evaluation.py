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


def test_real_index_excludes_poisoned_ids_and_resolves_current_canonical_notes():
    dataset = parse_dataset(raw_dataset())
    first = asyncio.run(evaluate(ContractOnlyEmbeddings(), dataset, evidence_kind="fake", limit=6))
    second = asyncio.run(evaluate(ContractOnlyEmbeddings(), dataset, evidence_kind="fake", limit=6))
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
    # k=6 deliberately retrieves every approved fixture, independent of fake ranks.
    for row in first["queries"]:
        assert len(row["retrieved_ids"]) == 6
        assert not row["forbidden_ids_returned"]
        results = {value["id"]: value for value in row["results"]}
        assert "水曜日" in results["observing-time"]["content"]
        assert results["observing-time"]["edited_since_approval"]
        assert "ESP32" in results["robot-current"]["content"]
        assert results["robot-current"]["origin"] == "user_explicit"
        assert results["robot-current"]["confidence"] == 0.8
        assert results["robot-current"]["source"].startswith("synthetic-evaluation:")
    assert first["queries"] == second["queries"]
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


def test_external_factory_provider_is_closed_after_private_failure(monkeypatch):
    class Provider:
        space = EmbeddingSpace("fixture/private-error", "v2", 2)
        closed = False

        async def embed(self, texts):
            raise RuntimeError("private sentinel: body, credential and path")

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
    )
    report = asyncio.run(run(args))
    assert provider.closed
    assert report["status"] == "failed"
    assert report["error"]["stage"] == "index_build"
    assert "private sentinel" not in json.dumps(report)
    assert report["contract"]["identifier"] == "fixture/private-error@v2:d2"
    assert report["runner"]["implementation_sha256"]


def test_cancel_propagates_and_owned_provider_closes(monkeypatch):
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
    command = [sys.executable, str(ROOT / "scripts/evaluate_memory.py"), "--output", str(report)]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(report.read_text())
    assert result["status"] == "completed"
    assert result["quality_assessment"] == "not_established"
    assert result["dataset"]["queries"] == 12
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
