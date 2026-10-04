"""Bounded, four-question artificial E5/Gemini connection trial (explicit opt-in)."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

from backend.chat.persistence import SQLiteConversationStore  # noqa: E402
from backend.chat.semantic_context import SemanticMemoryContext  # noqa: E402
from backend.chat.service import ChatService  # noqa: E402
from backend.core.config import Settings  # noqa: E402
from backend.memory.chroma import ChromaVectorIndex  # noqa: E402
from backend.memory.evaluation import (  # noqa: E402
    ContractOnlyEmbeddings,
    parse_dataset,
    prepare_synthetic_corpus,
)
from backend.memory.local_embedding import LocalE5Embeddings  # noqa: E402
from backend.memory.semantic import SemanticMemorySearcher  # noqa: E402
from backend.providers.factory import create_provider  # noqa: E402
from backend.providers.gemini import GeminiProvider  # noqa: E402

PLAN = REPO / "tests/fixtures/synthetic-gemini-questions-v1.json"
FIXTURE = REPO / "tests/fixtures/semantic-evaluation-ja-extra-v1.json"
MODEL = "gemini-2.5-flash"
MAX_REQUESTS = 4
MAX_OUTPUT_TOKENS = 1024


def load_plan():
    """No arbitrary corpus/query path; pin reviewed questions and artificial gold."""
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    if (
        plan["synthetic"] is not True
        or plan["model"] != MODEL
        or plan["max_requests"] != MAX_REQUESTS
        or plan["max_output_tokens"] != MAX_OUTPUT_TOKENS
        or len(plan["questions"]) != MAX_REQUESTS
        or hashlib.sha256(FIXTURE.read_bytes()).hexdigest() != plan["fixture_sha256"]
    ):
        raise ValueError("Frozen artificial Gemini plan changed")
    dataset = parse_dataset(json.loads(FIXTURE.read_text(encoding="utf-8")))
    for question in plan["questions"]:
        if question not in dataset.queries:
            raise ValueError("Question differs from reviewed artificial gold")
    return plan, dataset


def redact(text):
    """Also defend against an upstream response unexpectedly echoing the key."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        text = text.replace(key, "[REDACTED]")
    return re.sub(r"AIza[A-Za-z0-9_-]{20,}", "[REDACTED]", text)


class RecordedSemanticContext(SemanticMemoryContext):
    """Observe the existing verifier/renderer without modifying the chat prompt."""

    def __init__(self, searcher, ids):
        super().__init__(searcher)
        self.ids = ids
        self.context_json = None
        self.input_context = []
        self.candidates = []

    async def for_query(self, query):
        self.context_json = None
        self.input_context = []
        self.candidates = []
        return await super().for_query(query)

    def _render_result(self, result):
        # This runs only after SemanticMemoryContext's final current check.
        context = super()._render_result(result)
        self.context_json = context
        self.candidates = [
            {
                "id": str(match.record.id),
                "fixture_id": self.ids[str(match.record.id)],
                "revision": match.note_revision,
                "current_content": match.record.content,
                "source": match.record.source,
                "origin": match.record.origin.value,
                "confidence": match.record.confidence,
                "importance": match.record.importance,
                "edited_since_approval": match.edited_since_approval,
                "score": match.match_score,
            }
            for match in result.matches
        ]
        by_id = {item["id"]: item for item in self.candidates}
        self.input_context = [
            {"excerpt": item, "canonical": by_id[item["id"]]}
            for item in json.loads(context or "[]")
        ]
        return context


def observe_gemini_responses(provider):
    """Retain safe text/finish metadata even when the existing adapter rejects it.

    The isolated runner owns this adapter/client. Headers, URLs, full payloads and
    upstream error messages are never copied into evidence. No extra API calls.
    """
    records = []

    async def record_response(response):
        record = {"http_status": response.status_code, "response_text": None,
                  "finish_reason": None, "usage": {}, "parse_failure": None}
        try:
            await response.aread()
            payload = response.json()
            text, reason = GeminiProvider._content(payload)
            record["response_text"] = redact(text)
            record["finish_reason"] = redact(reason) if reason else None
            usage = payload.get("usageMetadata", {}) if isinstance(payload, dict) else {}
            if isinstance(usage, dict):
                record["usage"] = {
                    key: value for key, value in usage.items()
                    if key in {"promptTokenCount", "candidatesTokenCount",
                               "thoughtsTokenCount", "totalTokenCount"}
                    and isinstance(value, int) and not isinstance(value, bool)
                }
        except Exception as exc:
            record["parse_failure"] = type(exc).__name__
        records.append(record)

    if not isinstance(provider, GeminiProvider):
        raise ValueError("The artificial trial requires the existing Gemini adapter")
    provider._client.event_hooks["response"].append(record_response)
    return records


class CountedProvider:
    """A hard budget of four attempts; no retry or history from other questions."""

    def __init__(self, provider):
        self.provider = provider
        self.attempts = 0

    async def complete(self, request):
        if self.attempts >= MAX_REQUESTS:
            raise RuntimeError("Artificial request budget exhausted")
        self.attempts += 1
        return await self.provider.complete(request)


def fake_chat_factory(_settings, *, max_output_tokens=MAX_OUTPUT_TOKENS):
    def reply(_request):
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "fake contract response"}]},
                            "finishReason": "STOP"}],
        })

    return GeminiProvider(
        model=MODEL, api_key="contract-only-not-a-credential",
        max_output_tokens=max_output_tokens,
        client=httpx.AsyncClient(transport=httpx.MockTransport(reply)),
    )


def live_chat_factory(settings, *, max_output_tokens=MAX_OUTPUT_TOKENS):
    # Caller must explicitly set the model; arbitrary/default providers are ignored.
    if os.environ.get("JARVIS_GEMINI_MODEL") != MODEL:
        raise ValueError("Explicit gemini-2.5-flash model is required")
    return create_provider(settings, gemini_max_output_tokens=max_output_tokens)


async def _questions(retriever, embeddings, ids, chat, plan, responses):
    context = RecordedSemanticContext(SemanticMemorySearcher(retriever, embeddings), ids)
    store = SQLiteConversationStore(retriever.repository.database)
    counted = CountedProvider(chat)
    service = ChatService(counted, store, memory_context=context)
    rows = []
    for question in plan["questions"]:
        row = {"question": question, "answer": None, "failure": None}
        response_start = len(responses)
        try:
            # A new conversation for every question: no prior LLM answer in the next prompt.
            result = await service.complete(question["text"])
            row["answer"] = redact(result.reply)
            row["provider"] = result.provider
            row["model"] = result.model
        except Exception as exc:
            # Raw provider exceptions may contain keys or paths. Keep safe stage/type only.
            row["failure"] = {"type": type(exc).__name__, "detail": "Artificial turn failed"}
        row["api_responses"] = responses[response_start:]
        row["context_json"] = context.context_json
        row["context_characters"] = len(context.context_json or "")
        row["input_context"] = context.input_context
        row["search_candidates"] = context.candidates
        row["sources_provided"] = [item["canonical"]["source"] for item in context.input_context]
        # The existing provider response has no structured used-source attribution.
        # A citation proves only an explicit mention, not actual use or fact support.
        answer = row["answer"] or ""
        row["explicit_source_mentions"] = [
            source for source in row["sources_provided"] if source in answer
        ]
        row["used_sources"] = None
        row["used_sources_status"] = "manual_fact_to_source_review_required"
        row["fact_review"] = "pending; compare raw answer against current_content and frozen gold"
        rows.append(row)
    with retriever.repository.database.connect(read_only=True) as connection:
        saved = connection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
    return rows, counted.attempts, saved


def run_trial(
    embeddings, *, chat_factory=fake_chat_factory, evidence_kind="fake",
    max_output_tokens=MAX_OUTPUT_TOKENS,
):
    """Own temporary DB/vault/index and both providers even on failure/cancellation."""
    plan, dataset = load_plan()
    report = {
        "schema_version": 1, "synthetic": True, "evidence_kind": evidence_kind,
        "model": MODEL, "max_requests": MAX_REQUESTS,
        "max_output_tokens": max_output_tokens, "thinking_budget": "provider_default_dynamic",
        "plan_sha256": hashlib.sha256(PLAN.read_bytes()).hexdigest(),
        "fixture_sha256": plan["fixture_sha256"], "dataset_digest": dataset.digest,
        "started_at": datetime.now(UTC).isoformat(), "questions": [],
        "request_attempts": 0, "conversation_messages_saved": 0,
        "failure": None, "cleanup_failures": [],
        "quality_assessment": "not_established", "quality_thresholds": None,
        "original_notes_preserved": None,
    }
    try:
        report["source_head"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip()
        report["source_dirty"] = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=REPO, text=True
        ).strip())
        with (
            tempfile.TemporaryDirectory(prefix="jarvis-synthetic-gemini-") as directory,
            ExitStack() as resources,
            asyncio.Runner() as runner,
        ):
            chat = None
            try:
                def index_factory(path):
                    index = ChromaVectorIndex(path)
                    close = getattr(index.client, "close", None)
                    if close is not None:
                        resources.callback(close)
                    return index

                retriever, ids, challenge = runner.run(prepare_synthetic_corpus(
                    Path(directory), embeddings, dataset, index_factory=index_factory
                ))
                report["original_notes_preserved"] = challenge["original_notes_preserved"]
                report["embedding_space"] = embeddings.space.identifier
                settings = Settings(
                    db_path=Path(directory) / "memory.sqlite3", llm_provider="gemini",
                    memory_vault_path=Path(directory) / "vault",
                )
                chat = chat_factory(settings, max_output_tokens=max_output_tokens)
                responses = observe_gemini_responses(chat)
                rows, attempts, saved = runner.run(
                    _questions(retriever, embeddings, ids, chat, plan, responses)
                )
                report.update(questions=rows, request_attempts=attempts,
                              conversation_messages_saved=saved)
            except Exception as exc:
                report["failure"] = {"type": type(exc).__name__, "detail": "Trial setup failed"}
            finally:
                for provider in (chat, embeddings):
                    close = getattr(provider, "aclose", None)
                    if close is not None:
                        try:
                            runner.run(close())
                        except Exception as exc:
                            report["cleanup_failures"].append(type(exc).__name__)
    except Exception as exc:
        report["cleanup_failures"].append(type(exc).__name__)
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--contract-only", action="store_true", help="Fake vectors and fake HTTP")
    modes.add_argument(
        "--live", action="store_true", help="Explicit fixed E5 + up to 4 Gemini calls"
    )
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--max-output-tokens", type=int, default=MAX_OUTPUT_TOKENS)
    parser.add_argument("--output", type=Path, required=True, help="New JSON report path")
    args = parser.parse_args(argv)
    if args.contract_only and args.cache_dir is not None:
        parser.error("contract-only does not accept a model cache")
    if not 1 <= args.max_output_tokens <= 8192:
        parser.error("max-output-tokens must be between 1 and 8192")
    # Reserve before inference/API calls. Refuse overwrite and symlinks; mode 0600.
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        print("Use a new writable output path; existing evidence is preserved.", file=sys.stderr)
        return 2
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            if args.live and not os.environ.get("GEMINI_API_KEY", "").strip():
                plan, _ = load_plan()
                report = {"synthetic": True, "evidence_kind": "live", "status": "pending",
                          "reason": "No existing server-side Gemini credential available",
                          "request_attempts": 0, "plan": plan}
            elif args.live:
                cache = args.cache_dir or os.environ.get("JARVIS_LOCAL_MODEL_CACHE")
                if cache is None or not Path(cache).is_dir():
                    print("Prepared fixed E5 cache is required.", file=sys.stderr)
                    return 2
                if os.environ.get("JARVIS_GEMINI_MODEL") != MODEL:
                    print("Set explicit JARVIS_GEMINI_MODEL=gemini-2.5-flash.", file=sys.stderr)
                    return 2
                report = run_trial(LocalE5Embeddings(Path(cache)),
                                   chat_factory=live_chat_factory, evidence_kind="live",
                                   max_output_tokens=args.max_output_tokens)
            else:
                report = run_trial(
                    ContractOnlyEmbeddings(), max_output_tokens=args.max_output_tokens
                )
            output.write(redact(json.dumps(report, ensure_ascii=False, indent=2)) + "\n")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Artificial connection trial cancelled; no automatic retry.", file=sys.stderr)
        return 130
    except Exception:
        print("Artificial connection trial failed; provider details withheld.", file=sys.stderr)
        return 1
    failed = report.get("failure") or report.get("cleanup_failures") or any(
        row["failure"] for row in report.get("questions", [])
    )
    print("Artificial connection evidence written; fact/source review remains required.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
