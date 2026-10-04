"""New artificial connection boundaries: fake encoder and fake Gemini HTTP only."""

import asyncio
import json

import httpx
import pytest

from backend.core.config import ConfigError, Settings
from backend.memory.evaluation import ContractOnlyEmbeddings
from backend.providers.base import ChatMessage, CompletionRequest
from backend.providers.factory import create_provider
from backend.providers.gemini import GeminiProvider
from scripts import verify_synthetic_gemini as trial


class OwnedEmbeddings(ContractOnlyEmbeddings):
    closed = 0

    async def aclose(self):
        self.closed += 1


def observe_corpus(monkeypatch):
    observed = {}
    prepare = trial.prepare_synthetic_corpus

    async def wrapped(root, *args, **kwargs):
        observed["root"] = root
        result = await prepare(root, *args, **kwargs)
        observed["retriever"] = result[0]
        observed["canonical_snapshot"] = {
            p.name: p.read_bytes() for p in result[0].vault.root.glob("*.md")
        }
        return result

    monkeypatch.setattr(trial, "prepare_synthetic_corpus", wrapped)
    return observed


def factory_with_transport(reply, observed):
    def factory(_settings, *, max_output_tokens):
        client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        observed["client"] = client
        return GeminiProvider(model=trial.MODEL, api_key="contract-key", client=client,
                              max_output_tokens=max_output_tokens)
    return factory


def test_fixed_artificial_connection_captures_exact_bounded_input(monkeypatch):
    embeddings = OwnedEmbeddings()
    observed = observe_corpus(monkeypatch)
    requests = []

    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"candidates": [{
            "content": {"parts": [{"text": "fake response; no quality evidence"}]},
            "finishReason": "STOP",
        }]})

    report = trial.run_trial(embeddings, chat_factory=factory_with_transport(reply, observed),
                             max_output_tokens=4096)
    assert report["failure"] is None and report["cleanup_failures"] == []
    assert report["request_attempts"] == len(requests) == 4
    assert report["conversation_messages_saved"] == 8
    assert report["original_notes_preserved"] is True
    assert report["quality_assessment"] == "not_established"
    assert report["quality_thresholds"] is None
    assert embeddings.closed == 1 and observed["client"].is_closed
    assert not observed["root"].exists()
    _, dataset = trial.load_plan()
    expected = {n["id"]: n.get("edit_to", n["content"]) for n in dataset.notes
                if n["status"] == "approved"}
    for row, body in zip(report["questions"], requests, strict=True):
        assert body["generationConfig"] == {"maxOutputTokens": 4096}
        assert body["contents"][-1]["parts"][-1]["text"] == row["question"]["text"]
        # Only system instruction, memory and this user question: no prior answer.
        assert "fake response" not in json.dumps(body)
        reference = body["contents"][0]["parts"][0]["text"]
        assert reference.endswith(row["context_json"])
        assert row["context_characters"] <= 2400
        assert 1 <= len(row["input_context"]) <= 3
        for item in row["input_context"]:
            excerpt, canonical = item["excerpt"], item["canonical"]
            assert len(excerpt["content"]) <= 500 and len(excerpt["source"]) <= 200
            assert canonical["current_content"] == expected[canonical["fixture_id"]]
            assert canonical["revision"] and canonical["id"] == excerpt["id"]
            assert canonical["confidence"] == excerpt["confidence"] == 0.8
            assert canonical["source"].startswith("synthetic-evaluation:")
        assert row["used_sources"] is None  # Existing response has no source attribution.
        assert row["fact_review"].startswith("pending")


def test_recording_context_cannot_bypass_final_current_revision_check(monkeypatch):
    embeddings = OwnedEmbeddings()
    observed = observe_corpus(monkeypatch)
    search = trial.SemanticMemorySearcher.search
    calls = []

    async def edit_after_search(self, *args, **kwargs):
        result = await search(self, *args, **kwargs)
        note = self.retriever.vault.read(result.matches[0].record.id)
        self.retriever.vault.update(
            note.memory_id, note.body + " 人工の直後編集。", note.metadata,
            expected_revision=note.revision,
        )
        return result

    monkeypatch.setattr(trial.SemanticMemorySearcher, "search", edit_after_search)

    def reply(request):
        calls.append(request)
        raise AssertionError("A changed canonical note must block Gemini")

    report = trial.run_trial(embeddings, chat_factory=factory_with_transport(reply, observed))
    assert report["request_attempts"] == 0 and calls == []
    assert report["conversation_messages_saved"] == 0
    assert all(row["failure"]["type"] == "MemoryContextError" for row in report["questions"])
    assert all(row["context_json"] is None for row in report["questions"])
    assert embeddings.closed == 1 and observed["client"].is_closed
    assert not observed["root"].exists()


def test_provider_failure_preserves_canonical_and_history_and_closes_everything(monkeypatch):
    embeddings = OwnedEmbeddings()
    observed = observe_corpus(monkeypatch)
    calls = []
    monkeypatch.setenv("GEMINI_API_KEY", "private-key-sentinel")

    def reply(request):
        calls.append(request)
        retriever = observed["retriever"]
        assert {p.name: p.read_bytes() for p in retriever.vault.root.glob("*.md")} == (
            observed["canonical_snapshot"]
        )
        with retriever.repository.database.connect(read_only=True) as connection:
            rows = connection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
            assert rows == 0
        raise RuntimeError("private-key-sentinel /private/user-vault")

    report = trial.run_trial(embeddings, chat_factory=factory_with_transport(reply, observed))
    assert report["request_attempts"] == len(calls) == 4  # no automatic retries
    assert report["conversation_messages_saved"] == 0
    assert all(row["failure"]["type"] == "ProviderError" for row in report["questions"])
    serialized = json.dumps(report)
    assert "private-key-sentinel" not in serialized and "/private/user-vault" not in serialized
    assert embeddings.closed == 1 and observed["client"].is_closed
    assert not observed["root"].exists()


def test_budget_refuses_fifth_request():
    class Fake:
        async def complete(self, request):
            return request

    async def run():
        counted = trial.CountedProvider(Fake())
        for _ in range(4):
            await counted.complete("fake")
        with pytest.raises(RuntimeError, match="budget exhausted"):
            await counted.complete("fifth")
        assert counted.attempts == 4

    asyncio.run(run())


def test_missing_live_credentials_records_pending_without_encoder_or_user_storage(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "user.sqlite3"))
    monkeypatch.setenv("JARVIS_MEMORY_VAULT_PATH", str(tmp_path / "user-vault"))
    monkeypatch.setattr(trial, "LocalE5Embeddings", lambda *a: pytest.fail("No model needed"))
    output = tmp_path / "pending.json"
    assert trial.main(["--live", "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["status"] == "pending" and report["request_attempts"] == 0
    assert not (tmp_path / "user.sqlite3").exists() and not (tmp_path / "user-vault").exists()
    assert "credential" not in capsys.readouterr().out
    assert trial.main(["--live", "--output", str(output)]) == 2
    assert json.loads(output.read_text()) == report


@pytest.mark.parametrize("cap", [0, 8193, True, 1.5])
def test_invalid_gemini_output_limit_fails_before_client_creation(cap):
    with pytest.raises(ConfigError, match="output limit"):
        GeminiProvider(model=trial.MODEL, api_key="fake", max_output_tokens=cap)


def test_existing_factory_forwards_explicit_cap_only_for_gemini(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(GeminiProvider, "from_env", lambda **kw: captured.append(kw))
    create_provider(Settings(db_path=tmp_path / "fake.db", llm_provider="gemini"),
                    gemini_max_output_tokens=4096)
    assert captured == [{"max_output_tokens": 4096}]
    create_provider(Settings(db_path=tmp_path / "fake.db", llm_provider="none"))
    assert len(captured) == 1


@pytest.mark.parametrize("stream", [False, True])
def test_output_limit_applies_to_both_gemini_transports(stream):
    seen = []

    def reply(request):
        seen.append(json.loads(request.content))
        payload = {"candidates": [{"content": {"parts": [{"text": "fake"}]},
                                  "finishReason": "STOP"}]}
        if stream:
            return httpx.Response(200, text="data: " + json.dumps(payload) + "\n\n")
        return httpx.Response(200, json=payload)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            provider = GeminiProvider(model=trial.MODEL, api_key="fake", client=client,
                                      max_output_tokens=4096)
            request = CompletionRequest(messages=(ChatMessage("user", "artificial"),))
            if stream:
                assert [part async for part in provider.stream(request)] == ["fake"]
            else:
                assert (await provider.complete(request)).text == "fake"

    asyncio.run(run())
    assert seen[0]["generationConfig"] == {"maxOutputTokens": 4096}


def test_truncated_response_is_preserved_beside_failure_without_history(monkeypatch):
    embeddings = OwnedEmbeddings()
    observed = observe_corpus(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "echoed-key-sentinel")

    def reply(_request):
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [
                {"text": "hidden thought", "thought": True},
                {"text": "partial echoed-key-sentinel"},
            ]}, "finishReason": "MAX_TOKENS"}],
            "usageMetadata": {"totalTokenCount": 4096, "thoughtsTokenCount": 4000,
                              "unexpected": "echoed-key-sentinel"},
        })

    report = trial.run_trial(embeddings, chat_factory=factory_with_transport(reply, observed))
    assert report["request_attempts"] == 4 and report["conversation_messages_saved"] == 0
    for row in report["questions"]:
        assert row["answer"] is None and row["failure"]["type"] == "ProviderError"
        response, = row["api_responses"]
        assert response["response_text"] == "partial [REDACTED]"
        assert response["finish_reason"] == "MAX_TOKENS"
        assert response["usage"] == {"totalTokenCount": 4096, "thoughtsTokenCount": 4000}
    assert "hidden thought" not in json.dumps(report)
    assert "echoed-key-sentinel" not in json.dumps(report)
    assert embeddings.closed == 1 and observed["client"].is_closed


def test_cancellation_releases_owned_providers_and_temporary_data(monkeypatch):
    embeddings = OwnedEmbeddings()
    observed = observe_corpus(monkeypatch)

    def reply(_request):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        trial.run_trial(embeddings, chat_factory=factory_with_transport(reply, observed))
    assert embeddings.closed == 1 and observed["client"].is_closed
    assert not observed["root"].exists()
