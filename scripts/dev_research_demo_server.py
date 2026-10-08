"""DEV/TEST ONLY: serve the real app with fake demo research sessions for the Research screen.

Not imported by production code and not part of any gate. It starts the real `create_app` on
127.0.0.1 with a brand-new temporary SQLite file and seeds sessions only through the real
`ResearchRepository`. No search, reader, model or network call is made. It never reads `.env`
and never touches an existing database.

    python scripts/dev_research_demo_server.py [--port N]

Open the printed URL. Stop with Ctrl-C or SIGTERM.
"""

import argparse
import contextlib
import hashlib
import os
import shutil
import signal
import socket
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

RESERVED_PORTS = frozenset({8000, 8765, 8766, 18766})
DEFAULT_PORT = 18921

HOSTILE = (
    '<script>window.__pwned = "text"</script><img src=x onerror="window.__pwned=\'img\'">'
    "\n</div><b>bold?</b> [link](javascript:alert(1)) "
)


def free_port() -> int:
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port not in RESERVED_PORTS:
            return port
    raise RuntimeError("no free port found")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="0 picks a free port"
    )
    parser.add_argument("--db", type=Path, help="absolute path of a NEW sqlite file")
    return parser.parse_args(argv)


def seed(repository) -> None:
    from backend.research.models import (
        FailureReason,
        ResearchLevel,
        ResearchStatus,
        SourceEvaluation,
        SourceType,
    )

    now = datetime.now(UTC)

    def digest(name: str) -> str:
        return hashlib.sha256(name.encode()).hexdigest()

    def source(session, name, **values):
        defaults = {
            "url": f"https://docs.example.test/{name}",
            "final_url": f"https://docs.example.test/{name}",
            "retrieved_at": now - timedelta(minutes=5),
            "content_digest": digest(name),
        }
        defaults.update(values)
        return repository.add_source(session.id, **defaults)

    # 1. A completed session with sources, ratings and verified-quote claims.
    done = repository.create_session("Pythonのsqlite3モジュールで外部キー制約を有効にするには？",
                                     ResearchLevel.STANDARD)
    repository.transition(done.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    repository.add_query(done.id, "python sqlite3 foreign keys pragma")
    repository.add_query(done.id, "sqlite foreign key enforcement default off")
    official = source(
        done, "sqlite-foreign-keys",
        title="SQLite Foreign Key Support", publisher="Example SQLite Docs",
        published_at=now - timedelta(days=200), source_type=SourceType.OFFICIAL,
        evaluation=SourceEvaluation(
            authority=0.95, freshness=0.6, primary=1.0, relevance=0.9, agreement=0.8
        ),
    )
    blog = source(
        done, "blog-post", url="https://blog.example.test/sqlite-tips",
        final_url="https://blog.example.test/2026/sqlite-tips",
        title="Practical SQLite tips", publisher="Example Blog", source_type=SourceType.BLOG,
        evaluation=SourceEvaluation(authority=0.3, freshness=0.9, relevance=0.5),
    )
    repository.add_claim(
        done.id, claim_text="外部キー制約は接続ごとに PRAGMA で有効にする必要がある。",
        source_id=official.id, quote="foreign key constraints are disabled by default",
        quote_start=120, quote_end=167,
    )
    repository.add_claim(
        done.id, claim_text="デフォルトでは無効なので、接続のたびに実行する。",
        source_id=blog.id, quote="run PRAGMA foreign_keys = ON on every new connection",
    )
    repository.set_result(
        done.id,
        "SQLite の外部キー制約は既定で無効です。接続のたびに `PRAGMA foreign_keys = ON` を"
        "実行して有効にします [出典 1][出典 2]。",
    )

    # 2. A failed session (fixed failure code only).
    failed = repository.create_session("存在しない話題について調べる", ResearchLevel.QUICK)
    repository.transition(failed.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    repository.add_query(failed.id, "unfindable topic")
    repository.transition(failed.id, ResearchStatus.RUNNING, ResearchStatus.FAILED,
                          failure_reason=FailureReason.NO_RESULTS)

    # 3. A running session that already has partial data.
    running = repository.create_session("調査中のサンプル（まだ結果がありません）",
                                        ResearchLevel.DEEP)
    repository.transition(running.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    repository.add_query(running.id, "sample query in progress")
    source(running, "partial", title="Partially collected source",
           source_type=SourceType.UNKNOWN)

    # 4. Waiting, cancelled and pending sessions.
    waiting = repository.create_session("応答待ちのサンプル", ResearchLevel.EXTENSIVE)
    repository.transition(waiting.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    repository.transition(waiting.id, ResearchStatus.RUNNING, ResearchStatus.WAITING)
    cancelled = repository.create_session("取り消されたサンプル", ResearchLevel.MEMORY)
    repository.transition(cancelled.id, ResearchStatus.PENDING, ResearchStatus.CANCELLED)
    repository.create_session("まだ始まっていないサンプル")

    # 5. Hostile stored text: everything below must render as inert text.
    hostile = repository.create_session(HOSTILE + "A" * 240, ResearchLevel.STANDARD)
    repository.transition(hostile.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    repository.add_query(hostile.id, HOSTILE)
    evil = source(
        hostile, "evil", title=HOSTILE, publisher="<b>publisher</b>",
        evaluation=SourceEvaluation(authority=0.5),
    )
    repository.add_claim(
        hostile.id, claim_text=HOSTILE, source_id=evil.id,
        quote="<img src=x onerror=alert(1)> [x](javascript:alert(1)) " + "B" * 120,
    )
    repository.set_result(hostile.id, HOSTILE + "done")


def main(args: argparse.Namespace) -> None:
    workdir = None
    if args.db is None:
        workdir = Path(tempfile.mkdtemp(prefix="jarvis-research-demo-"))
        db_path = workdir / "demo.sqlite3"
    else:
        db_path = args.db
        if not db_path.is_absolute() or db_path.exists():
            raise SystemExit("--db must be an absolute path that does not exist yet")
    port = args.port or free_port()
    if port in RESERVED_PORTS or not 1024 <= port <= 65535:
        raise SystemExit("refusing a reserved or privileged port")

    # Pin the environment before the backend is imported, so even its import-time app object
    # can only ever point at the temporary database and no provider.
    os.environ["JARVIS_DB_PATH"] = str(db_path)
    os.environ["JARVIS_LLM_PROVIDER"] = "none"
    os.environ.pop("JARVIS_MEMORY_VAULT_PATH", None)

    import uvicorn

    from backend.api.app import create_app
    from backend.core.config import Settings
    from backend.core.database import Database
    from backend.research.repository import ResearchRepository

    database = Database(db_path)
    database.initialize()
    seed(ResearchRepository(database))

    config = uvicorn.Config(
        create_app(Settings(db_path=db_path)),
        host="127.0.0.1", port=port, log_level="warning", workers=1,
    )
    print(f"DEMO_URL=http://127.0.0.1:{port}/research  (db: temporary, loopback only)", flush=True)

    class DevServer(uvicorn.Server):
        # uvicorn re-raises SIGINT/SIGTERM with the default action once it stops, which would
        # skip the cleanup below. Plain exceptions let `finally` remove the temporary database.
        @contextlib.contextmanager
        def capture_signals(self):
            yield

    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    try:
        DevServer(config).run()
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    with contextlib.suppress(KeyboardInterrupt):
        main(parse_args(sys.argv[1:]))
