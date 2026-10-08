"""Run a synthetic retrieval evaluation; never imply a model quality pass."""

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from backend.memory.evaluation import (  # noqa: E402
    ContractOnlyEmbeddings,
    evaluate,
    evaluate_trials,
    parse_dataset,
)


def write_report(path: Path, report: dict) -> None:
    """Atomic new artifact, with no replacement of an earlier report/symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as file:
            temporary = Path(file.name)
            json.dump(report, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def run(args):
    provider = None
    report = {"schema_version": 1, "status": "failed", "quality_assessment": "not_established"}
    stage = "dataset_validation"
    try:
        data = args.fixture.read_bytes()
        dataset = parse_dataset(json.loads(data))
        stage = "provider_factory"
        if args.provider_factory:
            module, attribute = args.provider_factory.split(":", 1)
            provider = getattr(importlib.import_module(module), attribute)()
        else:
            provider = ContractOnlyEmbeddings()
        trials = getattr(args, "trials", 1)
        if trials == 1:
            report = await evaluate(
                provider, dataset, evidence_kind=args.evidence_kind, limit=args.limit
            )
        else:
            report = await evaluate_trials(
                provider, dataset, trials=trials, evidence_kind=args.evidence_kind, limit=args.limit
            )
        report["fixture_file_sha256"] = hashlib.sha256(data).hexdigest()
        head = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
        )
        status = subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain"], capture_output=True, text=True
        )
        report["runner"] = {
            "commit": head.stdout.strip() if head.returncode == 0 else None,
            "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "implementation_sha256": hashlib.sha256(
                (REPO / "backend/memory/evaluation.py").read_bytes()
            ).hexdigest(),
        }
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = {
            "stage": stage,
            "type": type(exc).__name__,
            "message": "Evaluation could not complete",
        }
    finally:
        close = getattr(provider, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:
                report["status"] = "failed"
                report["error"] = {
                    "stage": "provider_close",
                    "type": type(exc).__name__,
                    "message": "Evaluation provider could not close",
                }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", type=Path, default=REPO / "tests/fixtures/semantic-evaluation-ja-v1.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="1 to 20 fresh-index trials; default preserves a single report",
    )
    parser.add_argument(
        "--provider-factory", help="Explicit module:factory returning EmbeddingProvider"
    )
    parser.add_argument("--evidence-kind", choices=("fake", "model"))
    args = parser.parse_args()
    if not 1 <= args.trials <= 20:
        parser.error("trials must be between 1 and 20")
    if args.output.exists() or args.output.is_symlink():
        parser.error("Output already exists; choose a new report path")
    if args.provider_factory and (":" not in args.provider_factory or args.evidence_kind is None):
        parser.error("A provider factory requires module:factory and explicit evidence-kind")
    if not args.provider_factory:
        if args.evidence_kind not in {None, "fake"}:
            parser.error("Default contract-only vectors are fake evidence")
        args.evidence_kind = "fake"
    try:
        report = asyncio.run(run(args))
    except KeyboardInterrupt:
        report = {
            "schema_version": 1,
            "status": "cancelled",
            "quality_assessment": "not_established",
        }
    try:
        write_report(args.output, report)
    except OSError:
        print("Could not create a new evaluation report", file=sys.stderr)
        return 2
    print(f"Evaluation {report['status']}; quality not established. Report: {args.output}")
    if report["status"] == "cancelled":
        return 130
    return (
        0
        if (report["status"] == "completed" and report["summary"]["exclusion_violations"] == 0)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
