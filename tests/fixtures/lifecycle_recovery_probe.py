"""Inspect disposable canonical data from a fresh interpreter, without a provider."""

import json
import sys
from pathlib import Path
from uuid import UUID

from backend.core.database import Database
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository
from backend.memory.retrieval import MemoryRetriever, RetrievedMemory


def snapshot(database_path: Path, vault_path: Path) -> dict:
    database = Database(database_path)
    database.initialize()
    database.initialize()
    tables = (
        "schema_migrations",
        "conversations",
        "conversation_messages",
        "memory_records",
        "memory_review_events",
        "memory_lifecycle_events",
    )
    with database.connect(read_only=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        rows = {}
        for table in tables:
            ordering = "version" if table == "schema_migrations" else "id"
            query = f"SELECT * FROM {table} ORDER BY {ordering}"
            rows[table] = [dict(row) for row in connection.execute(query)]
    vault = ObsidianVault(vault_path)
    retriever = MemoryRetriever(MemoryRepository(database), vault)
    canonical = []
    notes = {}
    for row in rows["memory_records"]:
        memory_id = UUID(row["id"])
        note = vault.read(memory_id)
        if note is not None:
            notes[row["id"]] = note.revision
        memory = retriever.get_approved(memory_id)
        if isinstance(memory, RetrievedMemory):
            canonical.append(
                {
                    "id": row["id"],
                    "content": memory.record.content,
                    "source": memory.record.source,
                    "revision": memory.note_revision,
                }
            )
        else:
            assert memory is None  # Broken approved notes must fail this verification.
    return {"tables": rows, "notes": notes, "canonical": canonical}


if __name__ == "__main__":
    print(json.dumps(snapshot(Path(sys.argv[1]), Path(sys.argv[2])), ensure_ascii=False))
