"""Measure the casual path's latency on artificial small talk (JAR-103).

Run by the maintainer, in their own shell, with their own key:

    export GEMINI_API_KEY (your key) and JARVIS_GEMINI_MODEL=gemini-2.5-flash, then
    python scripts/measure_casual.py --output /path/outside/repo/casual-latency.json

Each turn is one artificial sentence sent through the real ``CasualService`` and, when
``--with-router`` is given, first through the real ``LLMRouter``. Only timings, character counts
and fixed codes are recorded: no prompt text, no reply text, no key. A missing key or an
unusable provider ends the run before any request. Evidence kind: live API.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from backend.chat.casual import CasualService  # noqa: E402
from backend.router.llm import LLMRouter  # noqa: E402

# Artificial small talk. Never replace these with real conversations.
TURNS = (
    "こんにちは！",
    "今日はいい天気だね。",
    "おはよう、調子はどう？",
    "ちょっと疲れたなあ。",
    "ありがとう、助かったよ。",
    "週末は何しようかな。",
    "おやすみなさい。",
    "最近、早起きを始めたんだ。",
    "コーヒーと紅茶ならどっちが好き？",
    "今日も一日お疲れさま。",
)


def summarize(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"count": 0, "median": None, "max": None}
    return {
        "count": len(values),
        "median": round(statistics.median(values), 3),
        "max": round(max(values), 3),
    }


async def measure_turn(service: CasualService, provider, router, text: str) -> dict:
    row: dict = {"router_seconds": None, "first_token_seconds": None, "total_seconds": None}
    try:
        if router is not None:
            began = time.perf_counter()
            decision = await router.decide(text)
            row["router_seconds"] = time.perf_counter() - began
            row["router_route"] = decision.route.value
            row["router_reason"] = decision.reason.value
        began = time.perf_counter()
        stream = await service.start(provider, service.request([], text))
        if stream is None:
            row["error"] = "no_first_token"
            return row
        row["first_token_seconds"] = time.perf_counter() - began
        chars = 0
        async for delta in stream.deltas():
            chars += len(delta)
        row["total_seconds"] = time.perf_counter() - began
        row["output_chars"] = chars
    except Exception as exc:  # Only the class name is kept: upstream text may echo input.
        row["error"] = type(exc).__name__
    return row


async def run(provider, *, repeat: int, with_router: bool) -> dict:
    service = CasualService()
    router = LLMRouter(provider) if with_router else None
    rows = []
    for _ in range(repeat):
        for text in TURNS:
            rows.append(await measure_turn(service, provider, router, text))
    ok = [r for r in rows if "error" not in r]
    report = {
        "schema_version": 1,
        "evidence_kind": "live_api",
        "provider": getattr(provider, "name", type(provider).__name__),
        "model": getattr(provider, "model", None),
        "turns": len(rows),
        "failed": len(rows) - len(ok),
        "failure_codes": sorted({r["error"] for r in rows if "error" in r}),
        "first_token_seconds": summarize([r["first_token_seconds"] for r in ok]),
        "total_seconds": summarize([r["total_seconds"] for r in ok]),
        "output_chars": summarize([float(r["output_chars"]) for r in ok]),
        "quality_assessment": "not_established",
    }
    if with_router:
        routed = [r for r in rows if r.get("router_seconds") is not None]
        report["router_seconds"] = summarize([r["router_seconds"] for r in routed])
        report["router_routes"] = {
            route: sum(1 for r in routed if r.get("router_route") == route)
            for route in sorted({r["router_route"] for r in routed})
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=1, help="passes over the 10 turns (1..3)")
    parser.add_argument("--with-router", action="store_true", help="also time LLMRouter.decide")
    args = parser.parse_args()
    if not 1 <= args.repeat <= 3:
        parser.error("repeat must be 1 to 3")
    if args.output.exists() or args.output.is_symlink():
        parser.error("Output already exists; choose a new report path")
    if REPO in args.output.resolve().parents:
        parser.error("Write the report outside the repository, then copy a reviewed summary")
    if not os.environ.get("GEMINI_API_KEY") or not os.environ.get("JARVIS_GEMINI_MODEL"):
        parser.error("Set GEMINI_API_KEY and JARVIS_GEMINI_MODEL in your own shell")
    from backend.providers.gemini import GeminiProvider

    async def go() -> dict:
        provider = GeminiProvider.from_env()
        try:
            return await run(provider, repeat=args.repeat, with_router=args.with_router)
        finally:
            await provider.aclose()

    report = asyncio.run(go())
    with args.output.open("x", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print(f"Wrote {args.output}: {report['turns']} turns, {report['failed']} failed")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
