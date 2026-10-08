"""Explicitly download the reviewed E5 safetensors snapshot outside the repo."""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from backend.memory.local_embedding import MODEL, REVISION  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.cache_dir.resolve().is_relative_to(REPO):
        parser.error("Keep the model cache outside the repository")
    if args.manifest.exists() or args.manifest.is_symlink():
        parser.error("Choose a new manifest path")
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from huggingface_hub import snapshot_download

    start = time.monotonic()
    try:
        snapshot = snapshot_download(
            MODEL,
            revision=REVISION,
            token=False,
            cache_dir=str(args.cache_dir),
            allow_patterns=[
                "config.json",
                "model.safetensors",
                "tokenizer.json",
                "tokenizer_config.json",
                "special_tokens_map.json",
                "sentencepiece.bpe.model",
            ],
        )
        files = {
            p.name: {
                "bytes": p.stat().st_size,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
            for p in Path(snapshot).iterdir()
            if p.is_file()
        }
        result = {
            "status": "completed",
            "model": MODEL,
            "revision": REVISION,
            "source": f"https://huggingface.co/{MODEL}/tree/{REVISION}",
            "files": files,
            "seconds": time.monotonic() - start,
        }
    except Exception as exc:
        result = {
            "status": "failed",
            "model": MODEL,
            "revision": REVISION,
            "error_type": type(exc).__name__,
            "message": "Local snapshot download failed",
        }
    try:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        with args.manifest.open("x") as file:
            json.dump(result, file, indent=2)
            file.write("\n")
    except OSError:
        print("Could not create a new manifest", file=sys.stderr)
        return 2
    print("Snapshot download " + result["status"])
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
