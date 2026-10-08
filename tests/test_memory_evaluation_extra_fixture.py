"""Protect the pre-inference extra gold lock without running an encoder."""

import hashlib
import json
from pathlib import Path

from backend.memory.evaluation import parse_dataset

ROOT = Path(__file__).resolve().parents[1]


def test_extra_fixture_matches_pre_inference_lock_and_preserves_prior_gold():
    lock = json.loads((ROOT / "docs/evidence/ja-extra-v1-gold-lock.json").read_text())
    fixture = ROOT / lock["fixture_path"]
    dataset = parse_dataset(json.loads(fixture.read_text()))
    assert dataset.identifier == lock["dataset_id"]
    assert dataset.digest == lock["canonical_sha256"]
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == lock["fixture_file_sha256"]
    assert len(dataset.queries) == 25
    assert lock["inference_before_lock"] is False
    assert lock["retrieval_results_seen_before_lock"] is False
    prior = lock["prior_fixture"]
    original = ROOT / prior["path"]
    assert parse_dataset(json.loads(original.read_text())).digest == prior["canonical_sha256"]
    assert hashlib.sha256(original.read_bytes()).hexdigest() == prior["fixture_file_sha256"]
