"""Interactive smoke tests use fake vectors, never real models or user storage."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from backend.memory.evaluation import ContractOnlyEmbeddings
from scripts import search_synthetic_memory as cli


class OwnedFake(ContractOnlyEmbeddings):
    def __init__(self):
        self.closed = 0
        self.queries = []

    async def embed_query(self, texts):
        self.queries.extend(texts)
        return await self.embed(texts)

    async def aclose(self):
        self.closed += 1


def lines(*values):
    iterator = iter(values)

    def read(prompt):
        try:
            value = next(iterator)
        except StopIteration:
            raise EOFError from None
        if isinstance(value, BaseException):
            raise value
        return value

    return read


def test_multiple_questions_prepare_once_ignore_empty_and_release_temp(monkeypatch):
    provider = OwnedFake()
    paths = []
    original = cli.prepare_synthetic_corpus

    async def prepare(root, *args, **kwargs):
        paths.append(root)
        return await original(root, *args, **kwargs)

    monkeypatch.setattr(cli, "prepare_synthetic_corpus", prepare)
    output = []
    status = cli.run_session(
        provider, limit=6, emit=output.append,
        read_line=lines(" ", "x" * 4001, "制御用のチップ", "観測会の曜日", "/quit"),
    )
    assert status == 0
    assert len(paths) == 1 and not paths[0].exists()
    assert provider.closed == 1
    assert provider.queries == ["制御用のチップ", "観測会の曜日"]
    text = "\n".join(output)
    assert "空入力" in text and "4000文字" in text
    assert "現在 approved" in text
    assert "出典: synthetic-evaluation:" in text
    assert "revision:" in text and "confidence: 0.8" in text
    assert "検索候補は回答ではありません" in text
    assert "robot-old (現在" not in text and "retired-venue (現在" not in text
    assert "pending-runtime (現在" not in text and "conflict-time (現在" not in text
    # Existing evaluation tests verify every retrieved body against fixture gold.
    # This smoke additionally checks the display of edited/corrected candidates
    # when ANN returns those artificial memories, without imposing ANN recall.
    if "[1] robot-current" in text or "robot-current (現在" in text:
        assert "ESP32" in text and "訂正元: robot-old" in text
    if "observing-time (現在" in text:
        assert "水曜日" in text and "承認後の編集: True" in text


@pytest.mark.parametrize(
    "ending", ["/exit", EOFError(), KeyboardInterrupt(), asyncio.CancelledError()]
)
def test_exit_and_cancel_close_provider_and_remove_temporary_corpus(monkeypatch, ending):
    provider = OwnedFake()
    paths = []

    async def prepare(root, *args, **kwargs):
        paths.append(root)
        return object(), {}, {}

    monkeypatch.setattr(cli, "prepare_synthetic_corpus", prepare)
    monkeypatch.setattr(cli, "SemanticMemorySearcher", lambda *args: object())
    if isinstance(ending, (KeyboardInterrupt, asyncio.CancelledError)):
        with pytest.raises(type(ending)):
            cli.run_session(provider, read_line=lines(ending), emit=lambda text: None)
    else:
        assert cli.run_session(provider, read_line=lines(ending), emit=lambda text: None) == 0
    assert provider.closed == 1
    assert len(paths) == 1 and not paths[0].exists()


@pytest.mark.parametrize("stage", ["preparation", "query"])
def test_inference_failure_is_redacted_and_resources_close(monkeypatch, stage):
    provider = OwnedFake()
    paths = []

    async def prepare(root, *args, **kwargs):
        paths.append(root)
        if stage == "preparation":
            raise RuntimeError("private path and payload sentinel")
        return object(), {}, {}

    class Searcher:
        def __init__(self, *args):
            pass

        async def search(self, *args, **kwargs):
            raise RuntimeError("private path and payload sentinel")

    monkeypatch.setattr(cli, "prepare_synthetic_corpus", prepare)
    monkeypatch.setattr(cli, "SemanticMemorySearcher", Searcher)
    output = []
    assert cli.run_session(provider, read_line=lines("質問", "/quit"), emit=output.append) == 1
    assert provider.closed == 1 and all(not root.exists() for root in paths)
    assert "sentinel" not in "\n".join(output)
    assert "できませんでした" in "\n".join(output)


def test_missing_cache_does_not_create_provider_or_read_user_db(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("JARVIS_LOCAL_MODEL_CACHE", raising=False)
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "real.sqlite3"))
    monkeypatch.setenv("JARVIS_MEMORY_VAULT_PATH", str(tmp_path / "real-vault"))
    monkeypatch.setattr(cli, "LocalE5Embeddings", lambda path: pytest.fail("must not allocate"))
    assert cli.main([]) == 2
    assert cli.main(["--cache-dir", str(tmp_path / "absent")]) == 2
    assert not list(tmp_path.iterdir())
    assert "cacheがありません" in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--db", "--vault", "--fixture", "--provider-factory"])
def test_real_storage_and_arbitrary_provider_arguments_are_rejected(option):
    with pytest.raises(SystemExit) as failure:
        cli.main([option, "/path"])
    assert failure.value.code == 2


def test_subprocess_fake_input_exit_and_environment_isolation(tmp_path):
    import os

    environment = dict(os.environ)
    environment["JARVIS_DB_PATH"] = str(tmp_path / "real.sqlite3")
    environment["JARVIS_MEMORY_VAULT_PATH"] = str(tmp_path / "real-vault")
    completed = subprocess.run(
        [sys.executable, str(Path(cli.__file__)), "--contract-only"],
        input="制御用のチップ\n/quit\n", text=True, capture_output=True, env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    assert "fake契約検証のみ" in completed.stdout
    assert "準備完了" in completed.stdout and "人工検索を終了" in completed.stdout
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("cancel", [False, True])
def test_index_client_closes_after_provider_and_before_temp_cleanup(monkeypatch, cancel):
    from types import SimpleNamespace

    events = []
    paths = []

    class Provider(OwnedFake):
        async def aclose(self):
            events.append("provider_close")
            await super().aclose()

    def index(path):
        paths.append(path.parent)

        def close():
            assert path.parent.exists()
            events.append("index_close")

        return SimpleNamespace(client=SimpleNamespace(close=close))

    async def prepare(root, *args, index_factory):
        index_factory(root / "index")
        if cancel:
            raise asyncio.CancelledError
        raise RuntimeError("preparation failed after creating index")

    monkeypatch.setattr(cli, "ChromaVectorIndex", index)
    monkeypatch.setattr(cli, "prepare_synthetic_corpus", prepare)
    provider = Provider()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            cli.run_session(provider, emit=lambda text: None)
    else:
        assert cli.run_session(provider, emit=lambda text: None) == 1
    assert events == ["provider_close", "index_close"]
    assert provider.closed == 1 and all(not p.exists() for p in paths)
