"""DEV/TEST ONLY: serve the real app with fake demo memories for the Memory screen.

Not imported by production code and not part of any gate. It starts the real `create_app` on
127.0.0.1 with a brand-new temporary SQLite file and seeds records only through the real
`MemoryRepository`. No vault, model or network call is made. It never reads `.env` and never
touches an existing database.

    python scripts/dev_memory_demo_server.py [--port N]

Open the printed URL. Stop with Ctrl-C or SIGTERM.
"""

import argparse
import contextlib
import os
import shutil
import signal
import socket
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

RESERVED_PORTS = frozenset({8000, 8765, 8766, 18766})
DEFAULT_PORT = 18931
DEMO_REVISION = "0123456789abcdef" * 4

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
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="0 picks a free port")
    parser.add_argument("--db", type=Path, help="absolute path of a NEW sqlite file")
    return parser.parse_args(argv)


def seed(repository) -> None:
    from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
    from backend.memory.repository import MemoryStatus

    start = datetime.now(UTC) - timedelta(days=3)
    counter = 0

    def add(content, *, approve=False, status=None, supersedes=None, **values):
        nonlocal counter
        counter += 1
        created = start + timedelta(hours=counter)
        defaults = {
            "category": MemoryCategory.USER,
            "source": "user:demo-conversation",
            "origin": MemoryOrigin.USER_EXPLICIT,
            "importance": 0.7,
            "confidence": 0.9,
            "created_at": created,
            "updated_at": created,
        }
        defaults.update(values)
        record = MemoryRecord(content=content, **defaults)
        if supersedes is None:
            repository.add(record)
        else:
            repository.add(record, supersedes_id=supersedes, supersedes_revision=DEMO_REVISION)
        if approve:
            repository.transition(
                record.id, expected=MemoryStatus.PENDING, new=MemoryStatus.APPROVED,
                vault_revision=DEMO_REVISION, actor="demo",
            )
        elif status is not None:
            repository.transition(record.id, expected=MemoryStatus.PENDING, new=status)
        return record

    # Approved notes.
    editor = add("エディタは Vim 系のキーバインドを好む。", approve=True, tags=("editor", "habit"))
    add(
        "JARVIS プロジェクトでは Python 3.11 と pytest を使う。", approve=True,
        category=MemoryCategory.PROJECT, project="jarvis", tags=("python",),
        source="user:demo-conversation:chars:10-42",
    )
    add(
        "朝のミーティングは 9 時台が多そうだ。", approve=True,
        origin=MemoryOrigin.AI_INFERENCE, confidence=0.4, importance=0.3,
        source="assistant:inference",
    )
    add(
        "ツール観測: 作業ディレクトリは git 管理下だった。", approve=True,
        category=MemoryCategory.WORK_STATE, origin=MemoryOrigin.TOOL_OBSERVATION,
        source="tool:demo",
    )

    # Candidates: pending, conflict, and a correction of an approved note.
    add("回答は簡潔な日本語が良い。", tags=("style",))
    add(
        "会議は毎週月曜の 10 時。", status=MemoryStatus.CONFLICT,
        origin=MemoryOrigin.AI_INFERENCE, confidence=0.5, source="assistant:inference",
    )
    add(
        "エディタは VS Code に変えた。", supersedes=editor.id,
        source="user:demo-correction", tags=("editor",),
    )

    # Hostile stored text: everything below must render as inert text.
    add(
        HOSTILE + "A" * 240, approve=True, source=HOSTILE, project=HOSTILE, tags=(HOSTILE,),
    )
    add(HOSTILE + "candidate", source=HOSTILE)


def main(args: argparse.Namespace) -> None:
    workdir = None
    if args.db is None:
        workdir = Path(tempfile.mkdtemp(prefix="jarvis-memory-demo-"))
        db_path = workdir / "demo.sqlite3"
    else:
        db_path = args.db
        if not db_path.is_absolute() or db_path.exists():
            raise SystemExit("--db must be an absolute path that does not exist yet")
    port = args.port or free_port()
    if port in RESERVED_PORTS or not 1024 <= port <= 65535:
        raise SystemExit("refusing a reserved or privileged port")

    # Pin the environment before the backend is imported, so even its import-time app object
    # can only ever point at the temporary database, no provider and no vault.
    os.environ["JARVIS_DB_PATH"] = str(db_path)
    os.environ["JARVIS_LLM_PROVIDER"] = "none"
    os.environ.pop("JARVIS_MEMORY_VAULT_PATH", None)

    import uvicorn

    from backend.api.app import create_app
    from backend.core.config import Settings
    from backend.core.database import Database
    from backend.memory.repository import MemoryRepository

    database = Database(db_path)
    database.initialize()
    seed(MemoryRepository(database))

    config = uvicorn.Config(
        create_app(Settings(db_path=db_path)),
        host="127.0.0.1", port=port, log_level="warning", workers=1,
    )
    print(f"DEMO_URL=http://127.0.0.1:{port}/memory  (db: temporary, loopback only)", flush=True)

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
