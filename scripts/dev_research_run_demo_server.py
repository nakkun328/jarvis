"""DEV/TEST ONLY: serve the real app with FAKE search, pages and model to try starting a research.

Not imported by production code and not part of any gate. It starts the real `create_app` on
127.0.0.1 with a brand-new temporary SQLite file, research switched on, and three fakes injected:
a canned search provider, a fake page transport behind the real safe reader, and a scripted
"model". No network call is made, `.env` is never read, and no key of any kind is used.

    python scripts/dev_research_run_demo_server.py [--port N] [--fast] [--research-off] [--chat]

Open the printed URL and type a question. Markers in the question pick the scenario:

    (none)       three agreeing pages -> a cited result
    #fail        every page fetch fails -> a failed run
    #slow        long pauses at every stage -> time to press Cancel or to see the busy refusal
    #hostile     page, title and quote contain markup and script-like text -> must render inert
    #conflict    pages disagree (60 s against 120 s) -> an open conflict is listed
    #nothing     the model cites nothing -> "no claim could be verified"

With --chat the chat page (/) is served too, with a FAKE router and a FAKE chat model, to try a
research started from the chat. Markers in the chat message pick the route: `#research` (or
"調べて") is decided research, `#casual` casual, `#lowconf` is a router fallback, anything else is
memory. The whole message becomes the research question, so the scenario markers above work in it
too. A turn answered by the Main Agent gets a fixed fake reply.

Stop with Ctrl-C or SIGTERM; the temporary database is removed.
"""

import argparse
import asyncio
import contextlib
import html
import json
import os
import re
import shutil
import signal
import socket
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

RESERVED_PORTS = frozenset({8000, 8765, 8766, 18766})
DEFAULT_PORT = 18971
PUBLIC_ADDRESS = "93.184.216.34"  # a documentation-range style public address; never connected to

SCENARIOS = ("fail", "slow", "hostile", "conflict", "nothing")
HOSTILE = (
    '<script>window.__pwned = "text"</script><img src=x onerror="window.__pwned=\'img\'"> '
    "[link](javascript:alert(1)) <b>bold?</b>"
)
CHAT_REPLY = "（デモ）これは偽のモデルによる通常の応答です。調査は行っていません。"
FACT = "The Foo widget cache keeps entries for 60 seconds."
OTHER_FACT = "The Foo widget cache keeps entries for 120 seconds."


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
    parser.add_argument("--fast", action="store_true", help="no pauses (smoke tests)")
    parser.add_argument(
        "--chat",
        action="store_true",
        help="also route chat turns with a fake router (see the module docstring)",
    )
    parser.add_argument(
        "--research-off",
        action="store_true",
        help="leave the switch off, to see the 'not configured' state of the screen",
    )
    return parser.parse_args(argv)


def scenario_of(text: str) -> str:
    for name in SCENARIOS:
        if f"#{name}" in text:
            return name
    return "agree"


def build_fakes(fast: bool, db_path: Path):
    """The fakes read the scenario marker from the question of the session being run."""
    from backend.core.database import Database
    from backend.providers.base import CompletionRequest, CompletionResponse
    from backend.research.mock_search import MockSearchProvider
    from backend.research.models import ResearchStatus
    from backend.research.reader import ReaderError, ReadFailure, TransportResponse
    from backend.research.repository import ResearchRepository
    from backend.research.search import SearchQuery

    repository = ResearchRepository(Database(db_path))

    def current_scenario() -> str:
        running = repository.list_sessions(ResearchStatus.RUNNING, limit=1)
        return scenario_of(running[0].question) if running else "agree"

    def pause(scenario: str) -> float:
        if fast:
            return 0.0
        return 8.0 if scenario == "slow" else 1.2

    def page(scenario: str, index: int) -> str:
        fact = {"conflict": OTHER_FACT if index == 1 else FACT, "hostile": f"{HOSTILE} {FACT}"}.get(
            scenario, FACT
        )
        title = f"Foo Cache {index} {HOSTILE}" if scenario == "hostile" else f"Foo Cache {index}"
        return (
            f"<html><head><title>{html.escape(title)}</title></head><body>"
            f"<h1>{html.escape(title)}</h1>"
            "<p>How long does the Foo widget cache keep entries?</p>"
            f"<p>FACT: {html.escape(fact)}</p></body></html>"
        )

    class DemoSearch:
        name = "demo"

        async def search(self, query: SearchQuery):
            scenario = current_scenario()
            await asyncio.sleep(pause(scenario))
            hits = [
                {
                    "url": f"https://docs{i}.demo.test/{scenario}/foo-cache",
                    "title": f"Foo Cache {i}",
                    "snippet": "A demo page about the Foo widget cache.",
                }
                for i in (1, 2, 3)
            ]
            return await MockSearchProvider({query.text: hits}).search(query)

    class DemoTransport:
        async def fetch(self, request) -> TransportResponse:
            match = re.fullmatch(r"https://docs(\d)\.demo\.test/(\w+)/foo-cache", request.url)
            if match is None:
                raise ReaderError(ReadFailure.HTTP_ERROR)
            index, scenario = int(match.group(1)), match.group(2)
            await asyncio.sleep(pause(scenario) / 2)
            if scenario == "fail":
                raise ReaderError(ReadFailure.NETWORK_ERROR)
            headers = {"content-type": "text/html; charset=utf-8"}
            return TransportResponse(200, headers, page(scenario, index).encode(), False)

    class DemoResolver:
        async def __call__(self, host: str) -> Sequence[str]:
            return [PUBLIC_ADDRESS]

    class DemoModel:
        """Cites the 'FACT:' line of every page it is shown, copied word for word.

        A chat turn (no evidence block in the request) gets a fixed fake reply.
        """

        name = "demo"
        model = "scripted"

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            if not any("<evidence" in message.content for message in request.messages):
                return CompletionResponse(text=CHAT_REPLY, provider="demo", model="scripted")
            user = request.messages[1].content
            scenario = current_scenario()
            await asyncio.sleep(pause(scenario))
            claims = []
            for number, body in re.findall(
                r'<evidence number="(\d+)">.*?text:\n(.*?)\n</evidence>', user, re.S
            ):
                fact = re.search(r"FACT: (.+)", body)
                if fact and scenario != "nothing":
                    quote = fact.group(1).strip()
                    claims.append({"text": quote, "source": int(number), "quote": quote})
            reply = {
                "answer": "(the demo model's own words are never shown)",
                "insufficient_evidence": scenario == "nothing",
                "claims": claims,
            }
            return CompletionResponse(text=json.dumps(reply), provider="demo", model="scripted")

        async def stream(self, request):
            # Only chat turns stream (research never does): the fixed fake reply, in two parts.
            half = len(CHAT_REPLY) // 2
            yield CHAT_REPLY[:half]
            yield CHAT_REPLY[half:]

    return DemoSearch(), DemoTransport(), DemoResolver(), DemoModel()


def build_router():
    """A fake router: the markers in the message pick the route (no model, no network)."""
    from backend.router import Route, RouteDecision, RouteReason, fallback

    class DemoRouter:
        async def decide(self, text: str) -> RouteDecision:
            lowered = text.lower()
            if "#lowconf" in lowered:
                return fallback(RouteReason.low_confidence, 0.2)
            if "#casual" in lowered:
                route = Route.casual
            elif "#research" in lowered or "調べて" in text:
                route = Route.research
            else:
                route = Route.memory
            return RouteDecision(route, 0.9, RouteReason.model_choice, False)

    return DemoRouter()


def main(args: argparse.Namespace) -> None:
    workdir = None
    if args.db is None:
        workdir = Path(tempfile.mkdtemp(prefix="jarvis-research-run-demo-"))
        db_path = workdir / "demo.sqlite3"
    else:
        db_path = args.db
        if not db_path.is_absolute() or db_path.exists():
            raise SystemExit("--db must be an absolute path that does not exist yet")
    port = args.port or free_port()
    if port in RESERVED_PORTS or not 1024 <= port <= 65535:
        raise SystemExit("refusing a reserved or privileged port")

    # Pin the environment before the backend is imported, so even its import-time app object
    # can only ever point at the temporary database, no provider, and no search key.
    os.environ["JARVIS_DB_PATH"] = str(db_path)
    os.environ["JARVIS_LLM_PROVIDER"] = "none"
    os.environ["JARVIS_SEARCH_PROVIDER"] = "none"
    os.environ["JARVIS_RESEARCH_ENABLED"] = "0"
    os.environ["JARVIS_ROUTER"] = "off"
    for name in ("JARVIS_SEARCH_API_KEY", "JARVIS_MEMORY_VAULT_PATH", "OPENAI_API_KEY"):
        os.environ.pop(name, None)

    import uvicorn

    from backend.api.app import create_app
    from backend.core.config import Settings
    from backend.research.reader import PageReader

    search, transport, resolver, model = build_fakes(args.fast, db_path)
    app = create_app(
        Settings(db_path=db_path, research_enabled=not args.research_off),
        model,
        search_provider=search,
        page_reader=PageReader(transport, resolver),
        router=build_router() if args.chat else None,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", workers=1)
    print(
        f"DEMO_URL=http://127.0.0.1:{port}/{'' if args.chat else 'research'}  "
        "(fake search, pages, model" + (" and router" if args.chat else "") + "; "
        "temporary database; loopback only)",
        flush=True,
    )

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
