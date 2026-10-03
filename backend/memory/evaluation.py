"""Synthetic retrieval diagnostics; completed runs are not quality acceptance."""

import hashlib
import json
import platform
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from backend.core.database import Database
from backend.memory.chroma import ChromaVectorIndex
from backend.memory.embedding import EmbeddingProvider, EmbeddingSpace, embed_texts
from backend.memory.indexing import MemoryIndexBuilder
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryStatus
from backend.memory.retrieval import MemoryRetriever
from backend.memory.semantic import SemanticMemorySearcher
from backend.memory.writer import MemoryWriter

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,119}\Z")
_STATES = {"approved", "pending", "conflict", "superseded", "retired"}
_KINDS = {
    "paraphrase",
    "revision",
    "correction",
    "unrelated",
    "insufficient_evidence",
    "retirement",
    "unapproved",
}


class EvaluationDataError(ValueError):
    """The synthetic gold set does not satisfy the versioned schema."""


@dataclass(frozen=True)
class EvaluationDataset:
    identifier: str
    digest: str
    notes: tuple[dict, ...]
    queries: tuple[dict, ...]


def _text(value, maximum=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise EvaluationDataError("Expected bounded nonempty text")
    return value


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise EvaluationDataError("Expected a stable dataset identifier")
    return value


def parse_dataset(raw: dict) -> EvaluationDataset:
    """Validate gold before allocating an index or calling an embedding model."""
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise EvaluationDataError("Unsupported evaluation schema")
    if isinstance(raw["schema_version"], bool) or raw.get("synthetic") is not True:
        raise EvaluationDataError("Evaluation requires an explicitly synthetic corpus")
    identifier = _identifier(raw.get("id"))
    notes, queries = raw.get("notes"), raw.get("queries")
    if not all(isinstance(x, list) and 1 <= len(x) <= 100 for x in (notes, queries)):
        raise EvaluationDataError("Expected 1 to 100 notes and queries")
    by_id = {}
    for note in notes:
        if not isinstance(note, dict):
            raise EvaluationDataError("Invalid note")
        key = _identifier(note.get("id"))
        if key in by_id or not isinstance(note.get("status"), str) or note["status"] not in _STATES:
            raise EvaluationDataError("Duplicate note ID or invalid review state")
        _text(note.get("content"))
        if "edit_to" in note:
            _text(note["edit_to"])
            if note["status"] != "approved":
                raise EvaluationDataError("Only approved notes have editable canonical bodies")
        by_id[key] = note
    replacements = []
    for note in notes:
        if note["status"] == "superseded":
            replacement = by_id.get(_identifier(note.get("replacement_id")))
            if replacement is None or replacement["status"] != "approved":
                raise EvaluationDataError("Correction requires one approved replacement")
            replacements.append(replacement["id"])
        elif "replacement_id" in note:
            raise EvaluationDataError("Only superseded notes identify a replacement")
    if len(set(replacements)) != len(replacements):
        raise EvaluationDataError("A replacement cannot correct two fixture notes")
    seen = set()
    for query in queries:
        if not isinstance(query, dict):
            raise EvaluationDataError("Invalid query")
        key = _identifier(query.get("id"))
        if key in seen or not isinstance(query.get("kind"), str) or query["kind"] not in _KINDS:
            raise EvaluationDataError("Duplicate query ID or invalid scenario")
        seen.add(key)
        _text(query.get("text"))
        _text(query.get("rationale"))
        for field in ("relevant_ids", "supporting_ids"):
            ids = query.get(field)
            if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
                raise EvaluationDataError("Gold IDs must be lists of identifiers")
            if len(set(ids)) != len(ids) or any(
                x not in by_id or by_id[x]["status"] != "approved" for x in ids
            ):
                raise EvaluationDataError("Gold may reference only unique approved notes")
        if not set(query["supporting_ids"]) <= set(query["relevant_ids"]):
            raise EvaluationDataError("Supporting evidence must also be relevant")
        if query["kind"] == "insufficient_evidence" and query["supporting_ids"]:
            raise EvaluationDataError("Insufficient-evidence gold cannot claim an answer source")
        if query["kind"] == "unrelated" and query["relevant_ids"]:
            raise EvaluationDataError("Unrelated gold cannot claim relevant notes")
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # Copy: caller mutation after validation must not change the labelled set.
    copied = json.loads(payload)
    return EvaluationDataset(
        identifier,
        hashlib.sha256(payload.encode()).hexdigest(),
        tuple(copied["notes"]),
        tuple(copied["queries"]),
    )


class ContractOnlyEmbeddings:
    """Deterministic arbitrary vectors; never a semantic or language model."""

    space = EmbeddingSpace("fixture/contract-only", "v1", 8)

    async def embed(self, texts):
        return [
            tuple((x + 1) / 256 for x in hashlib.sha256(t.encode()).digest()[:8]) for t in texts
        ]


def _mean(rows, field):
    values = [r["metrics"][field] for r in rows if r["metrics"][field] is not None]
    return sum(values) / len(values) if values else None


def _metrics(found, relevant, supporting, limit):
    hits = set(found) & set(relevant)
    ranks = [i for i, value in enumerate(found, 1) if value in relevant]
    return {
        "precision_at_k": len(hits) / limit if relevant else None,
        "recall_at_k": len(hits) / len(relevant) if relevant else None,
        "reciprocal_rank": 1 / ranks[0] if ranks else 0.0 if relevant else None,
        "support_recall_at_k": len(set(found) & set(supporting)) / len(supporting)
        if supporting
        else None,
        "negative_query_nonempty": bool(found) if not relevant else None,
        "supporting_gold_exists": bool(supporting),
    }


async def evaluate(
    provider: EmbeddingProvider,
    dataset: EvaluationDataset,
    *,
    evidence_kind: str,
    limit: int = 3,
) -> dict:
    """Use a disposable canonical corpus and supplied embedding contract.

    The caller owns the injected provider. No user database/vault path is
    accepted. Quality thresholds and downstream answer generation are absent.
    """
    if evidence_kind not in {"fake", "model"}:
        raise EvaluationDataError("Declare fake or model evidence explicitly")
    if isinstance(provider, ContractOnlyEmbeddings) and evidence_kind != "fake":
        raise EvaluationDataError("Contract-only vectors cannot be labelled model evidence")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise EvaluationDataError("limit must be between 1 and 100")
    report = {
        "schema_version": 1,
        "status": "running",
        "evidence_kind": evidence_kind,
        "quality_assessment": "not_established",
        "quality_thresholds": None,
        "dataset": {
            "id": dataset.identifier,
            "sha256": dataset.digest,
            "synthetic": True,
            "notes": len(dataset.notes),
            "queries": len(dataset.queries),
        },
        "started_at": datetime.now(UTC).isoformat(),
        "limit": limit,
        "contract": None,
        "retrieval_environment": {
            "index": "ChromaVectorIndex",
            "search": "approximate nearest neighbor; identical ranks are not guaranteed",
            "python": platform.python_version(),
            "platform": platform.system(),
            "chromadb": None,
        },
        "queries": [],
        "metric_definitions": {
            "precision_at_k": "relevant retrieved / configured k; positive gold only",
            "recall_at_k": "relevant retrieved / relevant gold; positive gold only",
            "reciprocal_rank": "1 / first relevant rank, or 0; positive gold only",
            "support_recall_at_k": "supporting retrieved / supporting gold; support gold only",
            "negative_query_nonempty": "retrieved context despite no relevant gold; diagnostic",
        },
    }
    stage = "contract"
    try:
        report["retrieval_environment"]["chromadb"] = version("chromadb")
        space = provider.space
        if not isinstance(space, EmbeddingSpace):
            raise EvaluationDataError("Provider must declare an embedding space")
        report["contract"] = {
            **asdict(space),
            "identifier": space.identifier,
            "provider_class": type(provider).__module__ + "." + type(provider).__qualname__,
        }
        with tempfile.TemporaryDirectory(prefix="jarvis-synthetic-evaluation-") as directory:
            stage = "fixture_setup"
            root = Path(directory)
            db = Database(root / "memory.sqlite3")
            db.initialize()
            repository = MemoryRepository(db)
            vault = ObsidianVault(root / "vault")
            writer = MemoryWriter(repository, vault)
            index = ChromaVectorIndex(root / "index")
            retriever = MemoryRetriever(repository, vault, vector_index=index)
            builder = MemoryIndexBuilder(repository, retriever, provider, index)
            records = {
                n["id"]: MemoryRecord(
                    id=uuid5(NAMESPACE_URL, f"jarvis-evaluation:{dataset.identifier}:{n['id']}"),
                    category=MemoryCategory.PROJECT,
                    content=n["content"],
                    source=f"synthetic-evaluation:{dataset.identifier}/{n['id']}",
                    origin=MemoryOrigin.USER_EXPLICIT,
                    importance=0.7,
                    confidence=0.8,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                    updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
                for n in dataset.notes
            }
            replacement_ids = {
                n["replacement_id"] for n in dataset.notes if n["status"] == "superseded"
            }
            for note in dataset.notes:
                if note["id"] in replacement_ids:
                    continue
                memory_id = records[note["id"]].id
                writer.submit(records[note["id"]])
                if note["status"] in {"approved", "superseded", "retired"}:
                    writer.approve(memory_id, actor="evaluation:synthetic")
                elif note["status"] == "conflict":
                    repository.transition(
                        memory_id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT
                    )
            stage = "index_build"
            await builder.populate_empty()
            stage = "review_challenges"
            for note in dataset.notes:
                memory_id = records[note["id"]].id
                if note["status"] == "superseded":
                    replacement = records[note["replacement_id"]]
                    writer.submit_correction(memory_id, replacement)
                    writer.approve(replacement.id, actor="evaluation:synthetic")
                    await builder.refresh_approved(replacement.id)
                elif note["status"] == "retired":
                    writer.retire(memory_id, actor="evaluation:synthetic", reason="Fixture review")
            # Deliberately retain inactive IDs and inject artificial unreviewed
            # IDs. This tests canonical filtering, not a healthy-cache claim.
            unreviewed = [n for n in dataset.notes if n["status"] in {"pending", "conflict"}]
            vectors = await embed_texts(provider, [n["content"] for n in unreviewed])
            await index.upsert(
                tuple(
                    space.record(str(records[n["id"]].id), vector)
                    for n, vector in zip(unreviewed, vectors, strict=True)
                )
            )
            for note in dataset.notes:
                if "edit_to" in note:
                    memory_id = records[note["id"]].id
                    current = vault.read(memory_id)
                    vault.update(
                        memory_id,
                        note["edit_to"],
                        current.metadata,
                        expected_revision=current.revision,
                    )
            audit = await builder.audit_ids()
            ids = {str(value.id): key for key, value in records.items()}
            report["index_challenge"] = {
                "intentionally_stale_or_unreviewed": True,
                "extra_ids": [ids[x] for x in audit.extra_ids],
                "stale_ids": [ids[x] for x in audit.stale_ids],
                "original_notes_preserved": all(
                    vault.read(records[n["id"]].id) is not None
                    for n in dataset.notes
                    if n["status"] in {"superseded", "retired"}
                ),
            }
            forbidden = {n["id"] for n in dataset.notes if n["status"] != "approved"}
            searcher = SemanticMemorySearcher(retriever, provider)
            for query in dataset.queries:
                stage = "query:" + query["id"]
                result = await searcher.search(query["text"], limit=limit)
                if result.issues:
                    raise EvaluationDataError("Canonical fixture could not be verified")
                found = [ids[str(match.record.id)] for match in result.matches]
                report["queries"].append(
                    {
                        **query,
                        "retrieved_ids": found,
                        "results": [
                            {
                                "id": ids[str(m.record.id)],
                                "score": m.match_score,
                                "content": m.record.content,
                                "source": m.record.source,
                                "origin": m.record.origin.value,
                                "confidence": m.record.confidence,
                                "importance": m.record.importance,
                                "revision": m.note_revision,
                                "edited_since_approval": m.edited_since_approval,
                                "stale": m.stale,
                            }
                            for m in result.matches
                        ],
                        "forbidden_ids_returned": sorted(set(found) & forbidden),
                        "metrics": _metrics(
                            found, query["relevant_ids"], query["supporting_ids"], limit
                        ),
                    }
                )
            rows = report["queries"]
            negative = [x for x in rows if not x["relevant_ids"]]
            unsupported = [x for x in rows if x["kind"] == "insufficient_evidence"]
            violations = sum(len(x["forbidden_ids_returned"]) for x in rows)
            report["summary"] = {
                "mean_precision_at_k": _mean(rows, "precision_at_k"),
                "mean_recall_at_k": _mean(rows, "recall_at_k"),
                "mean_reciprocal_rank": _mean(rows, "reciprocal_rank"),
                "mean_support_recall_at_k": _mean(rows, "support_recall_at_k"),
                "negative_queries": len(negative),
                "negative_nonempty": sum(bool(x["retrieved_ids"]) for x in negative),
                "insufficient_evidence_queries": len(unsupported),
                "insufficient_evidence_returning_context": sum(
                    bool(x["retrieved_ids"]) for x in unsupported
                ),
                "exclusion_violations": violations,
            }
            report["safety_status"] = (
                "violated" if violations else "no_exclusion_violation_observed"
            )
            report["status"] = "completed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = {
            "stage": stage,
            "type": type(exc).__name__,
            "message": "Evaluation could not complete",
        }
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report
