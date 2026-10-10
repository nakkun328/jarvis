"""Try the Tavily search adapter by hand, from your own terminal.

Reads JARVIS_SEARCH_API_KEY from the environment (never from arguments), runs a few
queries through the real provider and prints only rank, title, URL, snippet length and
published date, plus the credit usage the vendor reports for each call. It never prints the
key, the request body or the raw response. Each query costs one credit on the basic plan.

  python scripts/search_trial.py                       # three built-in Japanese queries
  python scripts/search_trial.py "query one" "query two"
  python scripts/search_trial.py --dry-run             # fake transport, no key, no network

These trial queries are not recorded in JARVIS' database, so the local monthly budget guard
does not count them. At most 10 queries are run per invocation.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

from backend.core.config import ConfigError  # noqa: E402
from backend.research.search import SearchError, SearchQuery  # noqa: E402
from backend.research.tavily import TavilySearchProvider  # noqa: E402

KEY_VARIABLE = "JARVIS_SEARCH_API_KEY"
MAX_QUERIES = 10
DEFAULT_QUERIES = ("東京の天気予報", "Python 3.13 の新機能", "富士山 登山 ルート")
DRY_RUN_CREDENTIAL = "dry-run-placeholder-credential"


def _dry_run_transport() -> httpx.MockTransport:
    """Artificial responses on reserved example domains; nothing leaves the process."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "results": [
                {
                    "title": "Example result one",
                    "url": "https://example.com/one",
                    "content": "An artificial snippet used for the dry run.",
                    "score": 0.9,
                    "published_date": "2026-01-02T03:04:05Z",
                },
                {
                    "title": "Example result two",
                    "url": "https://example.org/two",
                    "content": "Another artificial snippet.",
                    "score": 0.5,
                },
            ],
            "usage": {"credits": 1},
        }
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("queries", nargs="*", help="search queries (default: three built-in)")
    parser.add_argument("--max-results", type=int, default=3, help="results per query (1-10)")
    parser.add_argument(
        "--dry-run", action="store_true", help="use a fake transport; needs no key and no network"
    )
    return parser


async def _run(queries: Sequence[str], max_results: int, provider: TavilySearchProvider) -> int:
    total_credits = 0
    for index, text in enumerate(queries, start=1):
        print(f"[{index}/{len(queries)}]")
        try:
            results = await provider.search(SearchQuery(text, max_results=max_results))
        except SearchError as exc:
            print(f"Search failed ({exc.reason.value}). Stopped.", file=sys.stderr)
            return 1
        for result in results:
            published = result.published_at.date().isoformat() if result.published_at else "-"
            print(
                f"  {result.rank}. {result.title}\n"
                f"     {result.url}\n"
                f"     snippet: {len(result.snippet)} chars, published: {published}"
            )
        if not results:
            print("  (no results)")
        credits = provider.last_credits
        print(f"  credits used: {credits if credits is not None else 'not reported'}")
        total_credits += credits or 0
    print(f"Done: {len(queries)} queries, {total_credits} credits reported.")
    return 0


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    env = os.environ if environ is None else environ
    queries = list(args.queries) or list(DEFAULT_QUERIES)
    if len(queries) > MAX_QUERIES:
        print(f"Refusing to run more than {MAX_QUERIES} queries at once.", file=sys.stderr)
        return 2
    if not 1 <= args.max_results <= 10:
        print("--max-results must be between 1 and 10.", file=sys.stderr)
        return 2
    try:
        for text in queries:
            SearchQuery(text)
    except ValueError:
        print("Each query must be non-blank text of at most 500 characters.", file=sys.stderr)
        return 2
    if args.dry_run:
        provider = TavilySearchProvider(DRY_RUN_CREDENTIAL, transport=_dry_run_transport())
        print("Dry run: fake transport, no network, no key.")
    else:
        key = env.get(KEY_VARIABLE, "")
        if not key.strip():
            print(f"Set {KEY_VARIABLE} in your environment (docs/research-search.md).",
                  file=sys.stderr)
            return 2
        try:
            provider = TavilySearchProvider(key)
        except ConfigError:
            print(f"{KEY_VARIABLE} does not look like a valid key.", file=sys.stderr)
            return 2
    return asyncio.run(_run(queries, args.max_results, provider))


if __name__ == "__main__":
    raise SystemExit(main())
