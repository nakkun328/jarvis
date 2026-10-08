"""TEST/DEV ONLY: run the real JARVIS app with a scripted fake LLM provider.

This exists to exercise the chat UI in a real browser without any provider, key,
or network access. Production code never imports it. It refuses non-loopback
hosts, always uses the SQLite file you pass (create a fresh temp one), and never
reads `.env` or the process environment for provider settings.

    python scripts/dev_fake_provider_server.py --db /abs/tmp/jarvis.sqlite3 --port 8765
    python scripts/dev_fake_provider_server.py --db /abs/tmp/j.sqlite3 --no-provider

Type one of these as the chat message to choose the scripted behaviour:

    /slow            stream 40 chunks, 0.4s apart (for Stop / double-submit checks)
    /fail-now        provider error before the first delta
    /fail-after N    N deltas, then a provider error (default N=3)
    /flaky           fail before the first delta once, then succeed (for Retry)
    /empty           the provider stream ends without any text
    /history         reply with how many context messages the provider received
    anything else    a short reply streamed in a few chunks (0.15s apart)
"""

import argparse
import asyncio
import ipaddress
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.api.app import create_app  # noqa: E402
from backend.core.config import Settings  # noqa: E402
from backend.providers.base import (  # noqa: E402
    CompletionRequest,
    CompletionResponse,
    ProviderError,
)

_FAIL_AFTER = re.compile(r"^/fail-after(?:\s+(\d+))?$")


class ScriptedProvider:
    """Deterministic LLMProvider whose behaviour is chosen by the user message."""

    name = "fake"
    model = "scripted"

    def __init__(self, delay: float = 0.15, slow_delay: float = 0.4) -> None:
        self.delay = delay
        self.slow_delay = slow_delay
        self.requests: list[CompletionRequest] = []
        self._flaky_failed = False

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        text = "".join([chunk async for chunk in self.stream(request)])
        return CompletionResponse(text=text, provider=self.name, model=self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        self.requests.append(request)
        command = request.messages[-1].content.strip()
        # History = everything except the system prompt and the current user message.
        history = len(request.messages) - 2
        if command == "/fail-now":
            raise ProviderError("scripted failure before the first delta")
        if command == "/flaky" and not self._flaky_failed:
            self._flaky_failed = True
            raise ProviderError("scripted one-time failure")
        if command == "/empty":
            return
        if command == "/history":
            yield f"履歴メッセージ数: {history}"
            return
        if command == "/slow":
            for index in range(1, 41):
                await asyncio.sleep(self.slow_delay)
                yield f"遅い応答 {index}/40。"
            return
        match = _FAIL_AFTER.match(command)
        if match:
            for index in range(1, int(match.group(1) or 3) + 1):
                await asyncio.sleep(self.delay)
                yield f"途中の応答{index}。"
            raise ProviderError("scripted failure after partial output")
        for chunk in ("これは", "テスト用の", "応答です。", f"（履歴 {history} 件）"):
            await asyncio.sleep(self.delay)
            yield chunk


def build_dev_app(db_path: Path, *, with_provider: bool = True):
    if not db_path.is_absolute():
        raise ValueError("--db must be an absolute path to a temporary SQLite file")
    # llm_provider stays "none": the scripted provider is injected explicitly.
    settings = Settings(db_path=db_path)
    return create_app(settings, ScriptedProvider() if with_provider else None)


def _require_loopback(host: str) -> None:
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise SystemExit("refusing to bind: this dev harness only listens on loopback")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True, help="absolute temp SQLite path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--no-provider", action="store_true", help="serve with chat 503")
    args = parser.parse_args()
    _require_loopback(args.host)
    import uvicorn

    app = build_dev_app(args.db, with_provider=not args.no_provider)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
