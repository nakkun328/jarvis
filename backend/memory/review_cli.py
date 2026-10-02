"""Local, explicit review commands for durable memory candidates."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from backend.core.database import Database
from backend.memory.obsidian import ObsidianVault, VaultError
from backend.memory.repository import (
    MemoryRepository,
    MemoryRepositoryError,
    MemoryStatus,
    StoredMemory,
)
from backend.memory.writer import MemoryWriteError, MemoryWriter


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("memory ID must be a UUID") from exc


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be between 1 and 1000") from exc
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("limit must be between 1 and 1000")
    return number


def _summary(stored: StoredMemory, *, include_content: bool = False) -> dict[str, object]:
    record = stored.record
    result: dict[str, object] = {
        "id": str(record.id),
        "status": stored.status.value,
        "category": record.category.value,
        "source": record.source,
        "origin": record.origin.value,
        "importance": record.importance,
        "confidence": record.confidence,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "project": record.project,
        "tags": list(record.tags),
    }
    if include_content:
        result["content"] = record.content
        result["vault_revision"] = stored.vault_revision
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Existing JARVIS SQLite database")
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="List candidates in one review state")
    listing.add_argument(
        "--status", type=MemoryStatus, choices=list(MemoryStatus), default=MemoryStatus.PENDING
    )
    listing.add_argument("--limit", type=_limit, default=100)

    for name in ("show", "history", "approve", "reject", "flag-conflict"):
        command = commands.add_parser(name)
        command.add_argument("memory_id", type=_uuid)
        if name in ("approve", "reject", "flag-conflict"):
            command.add_argument("--actor", required=True, help="Operator label for the audit log")
        if name == "approve":
            command.add_argument("--vault", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Return 0 only when the requested review operation completed."""
    args = _parser().parse_args(argv)
    database = Database(args.db)
    if not database.is_ready():
        print("Database unavailable or schema not ready", file=sys.stderr)
        return 1
    repository = MemoryRepository(database)

    try:
        if args.command == "list":
            candidates = repository.list_by_status(args.status, limit=args.limit)
            print(json.dumps([_summary(item) for item in candidates], ensure_ascii=False))
            return 0

        stored = repository.get(args.memory_id)
        if stored is None:
            print("Memory candidate not found", file=sys.stderr)
            return 1
        if args.command == "show":
            print(json.dumps(_summary(stored, include_content=True), ensure_ascii=False))
            return 0
        if args.command == "history":
            events = repository.review_events(args.memory_id)
            print(
                json.dumps(
                    [
                        {
                            "id": event.id,
                            "memory_id": str(event.memory_id),
                            "previous_status": event.previous_status.value,
                            "new_status": event.new_status.value,
                            "action": event.action,
                            "actor": event.actor,
                            "occurred_at": event.occurred_at.isoformat(),
                            "vault_revision": event.vault_revision,
                        }
                        for event in events
                    ],
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "approve":
            updated = MemoryWriter(repository, ObsidianVault(args.vault)).approve(
                args.memory_id, actor=args.actor
            )
        else:
            new = MemoryStatus.REJECTED if args.command == "reject" else MemoryStatus.CONFLICT
            updated = repository.transition(
                args.memory_id, expected=stored.status, new=new, actor=args.actor
            )
        print(json.dumps(_summary(updated), ensure_ascii=False))
        return 0
    except (MemoryRepositoryError, MemoryWriteError, VaultError, ValueError) as exc:
        print(f"Review failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
