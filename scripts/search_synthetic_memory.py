"""Interactive local search of checked-in artificial memories only."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from backend.memory.chroma import ChromaVectorIndex  # noqa: E402
from backend.memory.evaluation import (  # noqa: E402
    ContractOnlyEmbeddings,
    parse_dataset,
    prepare_synthetic_corpus,
)
from backend.memory.local_embedding import LocalE5Embeddings  # noqa: E402
from backend.memory.semantic import SemanticMemorySearcher  # noqa: E402

FIXTURE = REPO / "tests/fixtures/semantic-evaluation-ja-v1.json"
NOTICE = (
    "人工データ専用・検索候補の表示です。候補が返っても回答根拠がある保証はありません。\n"
    "関連性と回答の支持根拠は別です。LLM回答生成なし、品質基準は未確立。\n"
    "質問を入力してください。/quit または /exit で終了、EOF/Ctrl-C で取消・終了。"
)


def display_result(result, ids, emit=print, *, corrections=None):
    """Show only resolved current approved bodies with provenance and revision."""
    if result.issues:
        emit("正本の検証に問題があり、この質問の候補を表示できません。")
        return False
    if not result.matches:
        emit("現在の承認済み記憶の候補はありません。")
    for rank, match in enumerate(result.matches, 1):
        record = match.record
        emit(f"[{rank}] {ids[str(record.id)]} (現在 approved)")
        emit(f"本文: {record.content}")
        emit(f"出典: {record.source}; origin: {record.origin.value}")
        emit(f"revision: {match.note_revision}")
        emit(
            f"confidence: {record.confidence}; importance: {record.importance}; "
            f"score: {match.match_score} (確率・confidenceではありません)"
        )
        emit(
            f"承認後の編集: {match.edited_since_approval}; stale: {match.stale}; "
            f"訂正元: {(corrections or {}).get(ids[str(record.id)], 'なし')}"
        )
    emit("検索候補は回答ではありません。質問への支持根拠を本文で確認してください。")
    return True


def run_session(provider, *, limit=3, read_line=input, emit=print):
    """Own the provider and one temporary corpus for multiple typed queries.

    Input stays on the main thread so EOF and Ctrl-C do not leave a blocked
    background stdin worker. Async preparation/search/close use one event loop.
    """
    failed = False
    try:
        # Unwind in this order: provider close, runner waits for worker threads,
        # index client close, then temporary files are removed.
        with (
            tempfile.TemporaryDirectory(prefix="jarvis-synthetic-search-") as directory,
            ExitStack() as resources,
            asyncio.Runner() as runner,
        ):
            try:
                dataset = parse_dataset(json.loads(FIXTURE.read_text(encoding="utf-8")))
                corrections = {
                    note["replacement_id"]: note["id"]
                    for note in dataset.notes if note["status"] == "superseded"
                }
                emit(NOTICE)
                emit("人工記憶と一時indexを準備しています。")
                def index_factory(path):
                    index = ChromaVectorIndex(path)
                    close = getattr(index.client, "close", None)
                    if close is not None:
                        resources.callback(close)
                    return index

                retriever, ids, _ = runner.run(
                    prepare_synthetic_corpus(
                        Path(directory), provider, dataset, index_factory=index_factory
                    )
                )
                searcher = SemanticMemorySearcher(retriever, provider)
                emit("準備完了。訂正・編集後の現在正本を検索します。")
                while True:
                    try:
                        text = read_line("質問> ")
                    except EOFError:
                        break
                    if text.strip() in {"/quit", "/exit"}:
                        break
                    if not text.strip():
                        emit("空入力です。質問を入力してください。")
                        continue
                    if len(text) > 4000:
                        emit("質問は4000文字以内にしてください。")
                        continue
                    try:
                        result = runner.run(searcher.search(text, limit=limit))
                        if not display_result(result, ids, emit, corrections=corrections):
                            failed = True
                    except Exception:
                        failed = True
                        emit("検索できませんでした。推論・index・正本を確認してください。")
            except Exception:
                failed = True
                emit("人工検索を準備できませんでした。固定cacheと依存環境を確認してください。")
            finally:
                close = getattr(provider, "aclose", None)
                if close is not None:
                    try:
                        runner.run(close())
                    except Exception:
                        failed = True
                        emit("検索資源の解放を完了できませんでした。")

    except Exception:
        failed = True
        emit("検索資源の解放または一時データの後片付けを完了できませんでした。")
    finally:
        emit("人工検索を終了しました。")
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir", type=Path,
        help="Prepared fixed E5 model cache; otherwise JARVIS_LOCAL_MODEL_CACHE",
    )
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument(
        "--contract-only", action="store_true",
        help="Explicit arbitrary fake-vector smoke test; not Japanese semantic search",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100:
        parser.error("limit must be between 1 and 100")
    if args.contract_only:
        if args.cache_dir is not None:
            parser.error("contract-only does not accept a model cache")
        provider = ContractOnlyEmbeddings()
        print("fake契約検証のみ: 日本語semanticモデルではありません。")
    else:
        configured = args.cache_dir or os.environ.get("JARVIS_LOCAL_MODEL_CACHE")
        if configured is None or not Path(configured).is_dir():
            print(
                "固定E5 cacheがありません。事前準備済みcacheを指定してください。",
                file=sys.stderr,
            )
            return 2
        provider = LocalE5Embeddings(Path(configured))
    try:
        return run_session(provider, limit=args.limit)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("人工検索を取り消しました。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
