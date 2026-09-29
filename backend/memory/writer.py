"""Coordinate reviewed memory candidates with editable Obsidian notes."""

from uuid import UUID

from backend.memory.model import MemoryRecord
from backend.memory.obsidian import ObsidianVault, VaultDocument, VaultError, VaultFormatError
from backend.memory.repository import (
    MemoryRepository,
    MemoryStateChanged,
    MemoryStatus,
    StoredMemory,
    validate_review_actor,
)
from backend.memory.retrieval import MemoryRetriever, RetrievedMemory


class MemoryWriteError(RuntimeError):
    """A memory could not be published consistently."""


class MemoryWriteConflict(MemoryWriteError):
    """The candidate or vault note needs review before publication."""


class MemoryWriter:
    """Stage candidates in SQLite and publish reviewed content to the vault.

    Creating a note and updating SQLite are not one atomic transaction. If the
    database update fails, an exact matching note is reused on retry; a changed
    note is left untouched and requires review.
    """

    def __init__(self, repository: MemoryRepository, vault: ObsidianVault) -> None:
        self.repository = repository
        self.vault = vault

    def submit(self, record: MemoryRecord) -> StoredMemory:
        """Stage a candidate without treating its content as an approved fact."""
        return self.repository.add(record)

    def submit_correction(self, old_id: UUID, record: MemoryRecord) -> StoredMemory:
        """Stage a replacement for human review; the old note remains current."""
        canonical = MemoryRetriever(self.repository, self.vault).get_approved(old_id)
        if not isinstance(canonical, RetrievedMemory):
            raise MemoryWriteConflict("Original approved note needs repair or review")
        return self.repository.add(
            record, supersedes_id=old_id, supersedes_revision=canonical.note_revision
        )

    def approve(self, memory_id: UUID, *, actor: str | None = None) -> StoredMemory:
        """Publish one explicitly reviewed candidate and record its note revision."""
        validate_review_actor(actor)
        stored = self.repository.get(memory_id)
        if stored is None:
            raise MemoryWriteConflict("Memory candidate does not exist")
        if stored.status is MemoryStatus.REJECTED:
            raise MemoryWriteConflict("Rejected memory cannot be approved")
        if stored.status is MemoryStatus.APPROVED:
            self._read_existing(memory_id)
            return stored
        if stored.status not in (MemoryStatus.PENDING, MemoryStatus.CONFLICT):
            raise MemoryWriteConflict("Inactive memory cannot be approved")

        record = stored.record
        metadata = _metadata(record)
        try:
            note = self.vault.create(memory_id, record.content, metadata)
        except FileExistsError:
            note = self._read_existing(memory_id)
        except (OSError, VaultError) as exc:
            raise MemoryWriteError("Memory vault unavailable") from exc

        if note.body != record.content or note.metadata != metadata:
            raise MemoryWriteConflict("Vault note differs from memory candidate")
        verified = self._read_existing(memory_id)
        if verified.revision != note.revision:
            raise MemoryWriteConflict("Vault note changed during publication")
        if stored.supersedes_id is not None:
            original = MemoryRetriever(self.repository, self.vault).get_approved(
                stored.supersedes_id
            )
            if (
                not isinstance(original, RetrievedMemory)
                or original.note_revision != stored.supersedes_revision
            ):
                raise MemoryWriteConflict("Original memory changed before correction approval")

        try:
            return self.repository.transition(
                memory_id,
                expected=stored.status,
                new=MemoryStatus.APPROVED,
                vault_revision=verified.revision,
                actor=actor,
            )
        except MemoryStateChanged as exc:
            raise MemoryWriteConflict("Memory review state changed during publication") from exc

    def retire(self, memory_id: UUID, *, actor: str, reason: str) -> StoredMemory:
        """Retire one canonical note after explicit review, without deleting history."""
        canonical = MemoryRetriever(self.repository, self.vault).get_approved(memory_id)
        if not isinstance(canonical, RetrievedMemory):
            raise MemoryWriteConflict("Original approved note needs repair or review")
        try:
            return self.repository.retire(
                memory_id,
                vault_revision=canonical.note_revision,
                actor=actor,
                reason=reason,
            )
        except MemoryStateChanged as exc:
            raise MemoryWriteConflict("Memory review state changed during retirement") from exc

    def _read_existing(self, memory_id: UUID) -> VaultDocument:
        try:
            note = self.vault.read(memory_id)
        except VaultFormatError as exc:
            raise MemoryWriteConflict("Memory vault note format needs review") from exc
        except (OSError, VaultError) as exc:
            raise MemoryWriteError("Memory vault unavailable or invalid") from exc
        if note is None:
            raise MemoryWriteConflict("Memory vault note is missing")
        return note


def _metadata(record: MemoryRecord) -> dict[str, str | float | list[str] | None]:
    return {
        "category": record.category.value,
        "source": record.source,
        "origin": record.origin.value,
        "importance": record.importance,
        "confidence": record.confidence,
        "created_at": record.created_at.isoformat(),
        "tags": list(record.tags),
        "project": record.project,
    }
