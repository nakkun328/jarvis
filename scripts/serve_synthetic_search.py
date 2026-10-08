"""Serve the explicit artificial-memory search UI on loopback only."""

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from backend.api.synthetic_search import create_synthetic_app  # noqa: E402
from backend.memory.evaluation import ContractOnlyEmbeddings  # noqa: E402
from backend.memory.local_embedding import LocalE5Embeddings  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, help="Existing fixed E5 cache (no download)")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--contract-only", action="store_true",
                        help="Explicit fake vector smoke test; no semantic quality evidence")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535 or args.port == 8765:
        parser.error("use a free port between 1024 and 65535, other than protected 8765")
    if not 1 <= args.limit <= 100:
        parser.error("limit must be between 1 and 100")
    if args.contract_only:
        if args.cache_dir is not None:
            parser.error("contract-only does not accept a model cache")
        factory = ContractOnlyEmbeddings
    else:
        configured = args.cache_dir or os.environ.get("JARVIS_LOCAL_MODEL_CACHE")
        if configured is None or not Path(configured).is_dir():
            print(
                "固定E5 cacheがありません。事前準備済みcacheを指定してください。", file=sys.stderr
            )
            return 2
        cache = Path(configured)
        factory = lambda: LocalE5Embeddings(cache)  # noqa: E731
    import uvicorn

    app = create_synthetic_app(factory, limit=args.limit, contract_only=args.contract_only)
    # Never enable reload or multiworker: one process owns the local CPU model.
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
