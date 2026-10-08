"""Synthetic browser boundaries, with contract vectors and disposable data only."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api import synthetic_search as browser
from backend.memory.evaluation import ContractOnlyEmbeddings
from scripts import serve_synthetic_search as cli


def client_for(app):
    # The app trusts only loopback Host headers, so tests use a loopback base URL.
    return TestClient(app, base_url="http://127.0.0.1")


class OwnedFake(ContractOnlyEmbeddings):
    def __init__(self):
        self.closed = 0

    async def aclose(self):
        self.closed += 1


def test_browser_reuses_current_approved_corpus_and_never_user_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "real.sqlite3"))
    monkeypatch.setenv("JARVIS_MEMORY_VAULT_PATH", str(tmp_path / "real-vault"))
    monkeypatch.setenv("JARVIS_LLM_PROVIDER", "openai")
    provider = OwnedFake()
    paths = []
    original = browser.prepare_synthetic_corpus

    async def prepare(root, *args, **kwargs):
        paths.append(root)
        return await original(root, *args, **kwargs)

    monkeypatch.setattr(browser, "prepare_synthetic_corpus", prepare)
    app = browser.create_synthetic_app(lambda: provider, limit=100, contract_only=True)
    with client_for(app) as client:
        assert client.get("/").status_code == 200
        assert "人工記憶の検索試験" in client.get("/").text
        assert client.get("/static/synthetic-search.js").status_code == 200
        state = client.get("/api/synthetic-search/status").json()
        assert state == {"contract_only": True, "prepared": False,
                         "synthetic": True, "fixture": "ja-extra-v1"}
        assert client.post("/api/chat", json={"message": "質問"}).status_code == 404
        response = client.post("/api/synthetic-search", json={"query": "制御用のチップ"})
        assert response.status_code == 200
        data = response.json()
        assert data["contract_only"] is True and data["support_assessment"] == "not_assessed"
        matches = {m["fixture_id"]: m for m in data["matches"]}
        assert "ESP32" in matches["mizuki-current"]["body"]
        assert matches["mizuki-current"]["corrects_fixture_id"] == "mizuki-old"
        assert "STM32" in matches["mitsuki-controller"]["body"]
        assert "水曜日の19時" in matches["observing-time"]["body"]
        assert matches["observing-time"]["edited_since_approval"] is True
        inactive = {"mizuki-old", "retired-venue", "pending-runtime", "conflict-time"}
        assert not inactive & matches.keys()
        for match in data["matches"]:
            assert match["source"].startswith("synthetic-evaluation:")
            assert match["revision"] and match["id"]
            assert match["memory_confidence"] == 0.8
            assert "index_score" in match and "confidence" not in match
        second = client.post("/api/synthetic-search", json={"query": "カナダの首都"})
        assert second.status_code == 200
        assert len(paths) == 1 and paths[0].exists()
    assert provider.closed == 1 and not paths[0].exists()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("query", ["", " \n ", "x" * 4001, 1])
def test_invalid_inputs_do_not_prepare_model(query):
    app = browser.create_synthetic_app(lambda: pytest.fail("must not prepare"))
    with client_for(app) as client:
        assert client.post("/api/synthetic-search", json={"query": query}).status_code == 422


@pytest.mark.parametrize("field", ["db", "vault", "fixture", "provider", "cache_dir"])
def test_http_storage_provider_and_fixture_paths_are_rejected(field):
    app = browser.create_synthetic_app(lambda: pytest.fail("must not prepare"))
    with client_for(app) as client:
        response = client.post("/api/synthetic-search", json={"query": "質問", field: "/path"})
        assert response.status_code == 422


def test_failed_preparation_releases_every_resource_and_retry_prepares_again(monkeypatch):
    providers, paths, events = [], [], []

    def factory():
        provider = OwnedFake()
        providers.append(provider)
        return provider

    def index(path):
        def close():
            assert path.parent.exists()
            events.append("index_close")
        return SimpleNamespace(client=SimpleNamespace(close=close))

    async def prepare(root, provider, dataset, *, index_factory):
        paths.append(root)
        index_factory(root / "index")
        if len(paths) == 1:
            raise RuntimeError("private raw exception /secret/path")
        return object(), {}, {}

    class Searcher:
        def __init__(self, *args):
            pass

        async def search(self, *args, **kwargs):
            return SimpleNamespace(issues=[], matches=[])

    monkeypatch.setattr(browser, "prepare_synthetic_corpus", prepare)
    monkeypatch.setattr(browser, "ChromaVectorIndex", index)
    monkeypatch.setattr(browser, "SemanticMemorySearcher", Searcher)
    with client_for(browser.create_synthetic_app(factory)) as client:
        failed = client.post("/api/synthetic-search", json={"query": "質問"})
        assert failed.status_code == 503 and "secret" not in failed.text
        assert providers[0].closed == 1 and not paths[0].exists()
        retried = client.post("/api/synthetic-search", json={"query": "質問"})
        assert retried.status_code == 200 and retried.json()["matches"] == []
        assert len(paths) == 2
    assert all(p.closed == 1 for p in providers) and all(not path.exists() for path in paths)
    assert events == ["index_close", "index_close"]


@pytest.mark.parametrize("failure", ["inference", "canonical"])
def test_search_failure_redacted_and_same_session_retries(monkeypatch, failure):
    provider = OwnedFake()
    count = 0

    async def prepare(*args, **kwargs):
        return object(), {}, {}

    class Searcher:
        def __init__(self, *args):
            pass

        async def search(self, *args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                if failure == "inference":
                    raise RuntimeError("private raw exception /secret/path")
                return SimpleNamespace(issues=["private issue"], matches=["must not display"])
            return SimpleNamespace(issues=[], matches=[])

    monkeypatch.setattr(browser, "prepare_synthetic_corpus", prepare)
    monkeypatch.setattr(browser, "SemanticMemorySearcher", Searcher)
    with client_for(browser.create_synthetic_app(lambda: provider)) as client:
        failed = client.post("/api/synthetic-search", json={"query": "質問"})
        assert failed.status_code == 503
        assert "secret" not in failed.text and "private" not in failed.text
        assert client.post("/api/synthetic-search", json={"query": "質問"}).status_code == 200
    assert provider.closed == 1


def test_shutdown_waits_for_query_then_closes_provider_index_and_temporary_data(monkeypatch):
    events = []
    paths = []

    class Provider(OwnedFake):
        async def aclose(self):
            events.append("provider_close")
            await super().aclose()

    async def run():
        started, finish = asyncio.Event(), asyncio.Event()

        async def prepare(root, *args, index_factory):
            paths.append(root)
            index_factory(root / "index")
            return object(), {}, {}

        def index(path):
            def close():
                assert path.parent.exists()
                events.append("index_close")
            return SimpleNamespace(client=SimpleNamespace(close=close))

        class Searcher:
            def __init__(self, *args):
                pass

            async def search(self, *args, **kwargs):
                started.set()
                await finish.wait()
                events.append("query_finished")
                return SimpleNamespace(issues=[], matches=[])

        monkeypatch.setattr(browser, "prepare_synthetic_corpus", prepare)
        monkeypatch.setattr(browser, "ChromaVectorIndex", index)
        monkeypatch.setattr(browser, "SemanticMemorySearcher", Searcher)
        session = browser.SyntheticSearchSession(Provider)
        searching = asyncio.create_task(session.search("質問"))
        await started.wait()
        closing = asyncio.create_task(session.aclose())
        await asyncio.sleep(0)
        assert not closing.done() and paths[0].exists()
        finish.set()
        await searching
        await closing
        with pytest.raises(RuntimeError, match="closed"):
            await session.search("質問")
    asyncio.run(run())
    assert events == ["query_finished", "provider_close", "index_close"]
    assert not paths[0].exists()


def test_cancelled_preparation_closes_provider_index_and_removes_temp(monkeypatch):
    events, paths = [], []

    class Provider(OwnedFake):
        async def aclose(self):
            events.append("provider_close")
            await super().aclose()

    def index(path):
        def close():
            assert path.parent.exists()
            events.append("index_close")
        return SimpleNamespace(client=SimpleNamespace(close=close))

    async def prepare(root, *args, index_factory):
        paths.append(root)
        index_factory(root / "index")
        raise asyncio.CancelledError

    monkeypatch.setattr(browser, "prepare_synthetic_corpus", prepare)
    monkeypatch.setattr(browser, "ChromaVectorIndex", index)

    async def run():
        session = browser.SyntheticSearchSession(Provider)
        with pytest.raises(asyncio.CancelledError):
            await session.search("質問")
        await session.aclose()

    asyncio.run(run())
    assert events == ["provider_close", "index_close"]
    assert not paths[0].exists()


@pytest.mark.parametrize("option", ["--db", "--vault", "--fixture", "--host", "--provider"])
def test_cli_rejects_storage_and_non_loopback_options(option):
    with pytest.raises(SystemExit) as failure:
        cli.main([option, "/path"])
    assert failure.value.code == 2


def test_cli_missing_cache_and_protected_port_never_start_server(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_LOCAL_MODEL_CACHE", raising=False)
    monkeypatch.setattr(cli, "LocalE5Embeddings", lambda path: pytest.fail("must not allocate"))
    assert cli.main([]) == 2
    assert cli.main(["--cache-dir", str(tmp_path / "absent")]) == 2
    with pytest.raises(SystemExit) as failure:
        cli.main(["--contract-only", "--port", "8765"])
    assert failure.value.code == 2


def test_cli_fake_entrypoint_loopback_only_and_no_normal_settings(monkeypatch):
    import uvicorn

    observed = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: observed.append(kwargs))
    assert cli.main(["--contract-only", "--port", "8799"]) == 0
    assert observed == [{"host": "127.0.0.1", "port": 8799, "access_log": False}]


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example", "192.168.0.5"])
def test_foreign_host_headers_are_rejected_before_any_route(host):
    app = browser.create_synthetic_app(lambda: pytest.fail("must not prepare"))
    with client_for(app) as client:
        for method, path in (("get", "/"), ("get", "/api/synthetic-search/status"),
                             ("post", "/api/synthetic-search")):
            kwargs = {"json": {"query": "質問"}} if method == "post" else {}
            response = getattr(client, method)(path, headers={"Host": host}, **kwargs)
            assert response.status_code == 400
        assert client.get("/", headers={"Host": "localhost:8766"}).status_code == 200


def test_contract_only_flag_is_reported_for_the_fake_label_and_real_mode_is_not_fake():
    for flag in (True, False):
        app = browser.create_synthetic_app(lambda: pytest.fail("must not prepare"),
                                           contract_only=flag)
        with client_for(app) as client:
            assert client.get("/api/synthetic-search/status").json()["contract_only"] is flag


@pytest.mark.parametrize("limit", [0, 101, True, "3", None])
def test_invalid_limit_is_rejected_before_any_resource(limit):
    with pytest.raises(ValueError):
        browser.create_synthetic_app(lambda: pytest.fail("must not prepare"), limit=limit)


def test_concurrent_first_searches_prepare_exactly_once(monkeypatch):
    paths = []
    original = browser.prepare_synthetic_corpus

    async def prepare(root, *args, **kwargs):
        paths.append(root)
        return await original(root, *args, **kwargs)

    monkeypatch.setattr(browser, "prepare_synthetic_corpus", prepare)

    async def run():
        provider = OwnedFake()
        session = browser.SyntheticSearchSession(lambda: provider, contract_only=True)
        try:
            first, second = await asyncio.gather(session.search("質問"), session.search("質問2"))
        finally:
            await session.aclose()
        return provider, first, second

    provider, first, second = asyncio.run(run())
    assert len(paths) == 1 and not paths[0].exists() and provider.closed == 1
    assert first["contract_only"] is second["contract_only"] is True


def test_cleanup_failure_still_releases_other_resources_and_stays_closed(monkeypatch):
    events = []

    class Provider(OwnedFake):
        async def aclose(self):
            events.append("provider_close")
            raise RuntimeError("private /secret/path")

    def index(path):
        return SimpleNamespace(client=SimpleNamespace(close=lambda: events.append("index_close")))

    async def prepare(root, *args, index_factory):
        index_factory(root / "index")
        events.append(root)
        return object(), {}, {}

    monkeypatch.setattr(browser, "prepare_synthetic_corpus", prepare)
    monkeypatch.setattr(browser, "ChromaVectorIndex", index)

    class Searcher:
        def __init__(self, *args):
            pass

        async def search(self, *args, **kwargs):
            return SimpleNamespace(issues=[], matches=[])

    monkeypatch.setattr(browser, "SemanticMemorySearcher", Searcher)

    async def run():
        session = browser.SyntheticSearchSession(Provider)
        await session.search("質問")
        with pytest.raises(RuntimeError, match="cleanup failed") as failure:
            await session.aclose()
        assert "secret" not in str(failure.value)
        with pytest.raises(RuntimeError, match="closed"):
            await session.search("質問")

    asyncio.run(run())
    assert events[1:] == ["provider_close", "index_close"] and not events[0].exists()


def test_cli_contract_only_rejects_model_cache_and_bad_limit(tmp_path):
    for argv in (["--contract-only", "--cache-dir", str(tmp_path)],
                 ["--contract-only", "--limit", "0"], ["--contract-only", "--limit", "101"],
                 ["--contract-only", "--port", "80"]):
        with pytest.raises(SystemExit) as failure:
            cli.main(argv)
        assert failure.value.code == 2
