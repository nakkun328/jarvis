"""DEV/TEST ONLY: serve the real app with fake demo tasks for looking at the Tasks screen.

Not imported by production code and not part of any gate. It starts the real `create_app` on
127.0.0.1 with a brand-new temporary SQLite file, then seeds tasks through the real
`TaskQueue` and `TaskRepository` using a fake executor (sleeps and writes progress; no real
tool, no network, no model). It never reads `.env` and never touches an existing database.

    python scripts/dev_tasks_demo_server.py [--port N] [--step-seconds S] [--repeat]

Open the printed URL. Stop with Ctrl-C or SIGTERM.
"""

import argparse
import asyncio
import contextlib
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

RESERVED_PORTS = frozenset({8000, 8765, 8766, 18766})

INJECTION_GOAL = (
    '<script>window.__pwned = "goal"</script><img src=x onerror="window.__pwned=\'img\'">'
    '\n</div><b>bold?</b> [link](javascript:alert(1)) '
    + "A" * 240
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
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--db", type=Path, help="absolute path of a NEW sqlite file")
    parser.add_argument("--step-seconds", type=float, default=6.0, help="slow task step length")
    parser.add_argument("--slow-steps", type=int, default=6)
    parser.add_argument("--repeat", action="store_true", help="keep starting new slow tasks")
    return parser.parse_args(argv)


async def main(args: argparse.Namespace) -> None:
    workdir = None
    if args.db is None:
        workdir = Path(tempfile.mkdtemp(prefix="jarvis-tasks-demo-"))
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
    from backend.tasks.models import StepStatus, WaitingReason
    from backend.tasks.queue import (
        ExecutionOutcome,
        TaskQueue,
        VerificationResult,
    )
    from backend.tasks.repository import TaskRepository

    settings = Settings(db_path=db_path)
    database = Database(db_path)
    database.initialize()
    repository = TaskRepository(database)
    queue = TaskQueue(repository, poll_interval_seconds=0.02)

    class FakeExecutor:
        """Walks every step with a pause; `end` decides how the run finishes."""

        def __init__(self, *, pause=0.05, stop_after=None, end="success", note_html=False):
            self.pause, self.stop_after, self.end, self.note_html = (
                pause, stop_after, end, note_html,
            )

        async def execute(self, task, progress, cancellation):
            for step in task.steps:
                if self.stop_after is not None and step.index >= self.stop_after:
                    break
                progress.start_step(step.index)
                await asyncio.sleep(self.pause)
                note = f"手順 {step.index + 1} を確認しました"
                if self.note_html:
                    note = "<img src=x onerror=alert(1)> <script>alert(2)</script>"
                if self.end == "fail" and step.index == self.stop_after - 1:
                    progress.finish_step(step.index, status=StepStatus.FAILED, note="失敗")
                    return ExecutionOutcome.failure()
                progress.finish_step(step.index, note=note)
            if self.end == "wait":
                return ExecutionOutcome.wait(WaitingReason.NEEDS_CONFIRMATION)
            if self.end == "hang":
                await asyncio.sleep(60)
            return ExecutionOutcome.succeeded(
                "<b>完了</b> <script>window.__pwned='summary'</script> デモの結果です。"
                if self.note_html
                else "デモの結果の要約です。"
            )

    class Verifier:
        def __init__(self, passed=True):
            self.passed = passed

        async def verify(self, task, outcome):
            return VerificationResult(self.passed)

    steps3 = ["状態を確認する", "変更を準備する", "結果を確かめる"]

    # An interrupted task: running when "the process died", then recovered as failed.
    queue.submit("再起動で中断されたタスク（デモ）", ["準備", "実行"])
    claimed = repository.claim_next()
    repository.start_step(claimed.id, 0)
    repository.recover_in_flight()

    async def run(goal, steps, executor, verifier=None, **kwargs):
        queue.submit(goal, steps, target_device="demo-device")
        await queue.run_next(executor, verifier or Verifier(), **kwargs)

    await run("完了するデモタスク", steps3, FakeExecutor())
    await run("実行に失敗するデモタスク", steps3, FakeExecutor(stop_after=2, end="fail"))
    await run("結果の検証に失敗するデモタスク", steps3, FakeExecutor(), Verifier(False))
    await run("時間切れになるデモタスク", steps3, FakeExecutor(stop_after=1, end="hang"),
              timeout_seconds=0.3)
    await run("確認待ちのデモタスク", steps3, FakeExecutor(stop_after=1, end="wait"))
    cancelled = queue.submit("取り消されたデモタスク", ["準備", "実行"])
    queue.cancel(cancelled.id)
    await run(INJECTION_GOAL, ["<b>手順</b> <script>x</script>", "手順 2"],
              FakeExecutor(note_html=True))

    class SlowExecutor(FakeExecutor):
        async def execute(self, task, progress, cancellation):
            for step in task.steps:
                progress.start_step(step.index)
                await asyncio.sleep(args.step_seconds)
                progress.finish_step(step.index, note=f"手順 {step.index + 1} 完了")
            return ExecutionOutcome.succeeded("ゆっくり実行したデモの結果です。")

    slow_steps = [f"ゆっくり進める手順 {i + 1}" for i in range(args.slow_steps)]
    runs = 0

    async def slow_worker():
        nonlocal runs
        while True:
            runs += 1
            goal = f"実行中のデモタスク #{runs}（手順は {args.step_seconds:g} 秒ごと）"
            queue.submit(goal, slow_steps)
            await queue.run_next(SlowExecutor(), Verifier())
            if not args.repeat:
                return
            await asyncio.sleep(2)

    worker = asyncio.create_task(slow_worker())
    await asyncio.sleep(0.5)
    queue.submit("待機中のデモタスク（まだ開始されていません）", ["最初の手順", "次の手順"])

    config = uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=port, log_level="warning", workers=1
    )
    server = uvicorn.Server(config)
    print(f"DEMO_URL=http://127.0.0.1:{port}/tasks  (db: temporary, loopback only)", flush=True)
    try:
        await server.serve()
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(parse_args(sys.argv[1:])))
