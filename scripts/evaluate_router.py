"""Run a router evaluation on the artificial set; never imply a quality pass.

Built-in routers are offline: ``rule`` (keyword baseline) and ``contract-only``
(always falls back). A model-backed router can only be supplied through an
explicit ``--router-factory`` with ``--evidence-kind``; this script never reads
keys or starts a provider itself.
"""

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

from backend.router.evaluation import (  # noqa: E402
    ContractOnlyRouter,
    evaluate,
    parse_router_dataset,
)
from backend.router.rule import RuleRouter  # noqa: E402

BUILTIN = {
    "rule": (RuleRouter, "rule_baseline"),
    "contract-only": (ContractOnlyRouter, "fake"),
}


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
    report = {"schema_version": 1, "status": "failed", "quality_assessment": "not_established"}
    stage = "dataset_validation"
    router = None
    try:
        data = args.fixture.read_bytes()
        dataset = parse_router_dataset(json.loads(data))
        stage = "router_factory"
        if args.router_factory:
            module, attribute = args.router_factory.split(":", 1)
            router = getattr(importlib.import_module(module), attribute)()
            name, evidence_kind = args.router_factory, args.evidence_kind
        else:
            factory, evidence_kind = BUILTIN[args.router]
            router, name = factory(), args.router
        stage = "evaluation"
        report = await evaluate(
            router, dataset, router_name=name, evidence_kind=evidence_kind, limit=args.limit
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
                (REPO / "backend/router/evaluation.py").read_bytes()
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
        close = getattr(router, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:
                report["status"] = "failed"
                report["error"] = {
                    "stage": "router_close",
                    "type": type(exc).__name__,
                    "message": "Evaluation router could not close",
                }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", type=Path, default=REPO / "tests/fixtures/router-eval-ja-v1.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None, help="Evaluate only the first N turns")
    parser.add_argument("--router", choices=sorted(BUILTIN), default="rule")
    parser.add_argument(
        "--router-factory", help="Explicit module:factory returning a Router (no-argument call)"
    )
    parser.add_argument("--evidence-kind", choices=("fake", "model"))
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("limit must be positive")
    if args.output.exists() or args.output.is_symlink():
        parser.error("Output already exists; choose a new report path")
    if args.router_factory:
        if ":" not in args.router_factory or args.evidence_kind is None:
            parser.error("A router factory requires module:factory and explicit evidence-kind")
    elif args.evidence_kind is not None:
        parser.error("Built-in routers set their own evidence kind")
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
    ok = report["status"] == "completed" and report["summary"]["router_contract_violations"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
